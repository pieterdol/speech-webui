#!/usr/bin/env python3
"""
speech-webui — a phone-friendly web front-end for the local speech tools.

  speech → text   faster-whisper (in-process, model kept resident)
  text → speech   Kokoro (54 built-in voices, seconds) or F5-TTS (clones a clip, minutes)

Both TTS engines live in their own venvs, so we shell out to the CLIs in ~/.local/bin
instead of duplicating their dependencies.

Reach it from your phone over Tailscale at https://your-machine.your-tailnet.ts.net:8443
(HTTPS is required — Safari blocks the microphone and the clipboard on plain http).
"""
import json, os, re, shutil, subprocess, tempfile, threading, time, uuid
from flask import Flask, Response, jsonify, request, send_from_directory

HERE         = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR    = os.path.join(HERE, "clips")
OUT_DIR      = os.path.join(HERE, "outputs")
PRESETS_DIR  = os.path.join(HERE, "presets")
INDEX_FILE   = os.path.join(HERE, "clips.json")
PRESETS_FILE = os.path.join(HERE, "presets.json")
PORT         = int(os.environ.get("SPEECH_PORT", "8600"))

KOKORO = os.path.expanduser("~/.local/bin/kokoro-tts")
F5     = os.path.expanduser("~/.local/bin/f5-tts")

os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(PRESETS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB — long recordings are fine

run_lock   = threading.Lock()   # one model at a time: F5-TTS saturates all 12 cores
index_lock = threading.Lock()
jobs       = {}                 # job_id -> state dict (polled by /api/status)

STT_MODELS  = ("small", "large-v3-turbo")
DEFAULT_STT = "small"
LANGS       = ("en", "nl")

# Kokoro voice prefix -> language code. Without this a British voice is phonemized
# with US rules and sounds wrong.
VOICE_LANG = {"af":"en-us", "am":"en-us", "bf":"en-gb", "bm":"en-gb", "jf":"ja", "jm":"ja",
              "zf":"cmn", "zm":"cmn", "ef":"es", "em":"es", "ff":"fr-fr",
              "hf":"hi", "hm":"hi", "if":"it", "im":"it", "pf":"pt-br", "pm":"pt-br"}

# Neither engine has a pronunciation-override syntax, so hard words are respelled
# phonetically on the way in.
RESPELL = {"Pieter": "Peter"}

REF_TRIM_SECONDS = 10   # F5 gains nothing from a longer reference
NFE_STEPS        = (16, 32)   # sampling steps: 16 is ~2x faster, 32 is the default quality

def respell(text):
    for src, dst in RESPELL.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
    return text

# ---- clip index (clips.json) ----
def load_index():
    try:
        with open(INDEX_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_index(items):
    with open(INDEX_FILE, "w") as f: json.dump(items, f, indent=2)

def find_clip(clip_id):
    return next((c for c in load_index() if c.get("id") == clip_id), None)

def clip_path(clip):
    return os.path.join(CLIPS_DIR, clip["file"])

# ---- voice presets (presets.json) ----
# A preset owns its own copy of the audio, already trimmed, so it keeps working after
# the clip it came from is deleted — clips are scratch, presets are meant to last.
def load_presets():
    try:
        with open(PRESETS_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_presets(items):
    with open(PRESETS_FILE, "w") as f: json.dump(items, f, indent=2)

def find_preset(preset_id):
    return next((p for p in load_presets() if p.get("id") == preset_id), None)

def save_transcript(clip_id, text):
    with index_lock:
        items = load_index()
        for c in items:
            if c.get("id") == clip_id: c["transcript"] = text
        write_index(items)

# ---- audio helpers ----
def audio_seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=30)
        return round(float(r.stdout.strip()), 1)
    except Exception:
        return 0.0

def normalize_audio(src, dst, seconds=None):
    """Decode anything the browser or phone produces (iOS audio/mp4, Chrome webm/opus,
    m4a/mp3/flac uploads) into 24 kHz mono 16-bit wav — what Whisper and F5-TTS both
    want internally."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", src]
    if seconds: cmd += ["-t", str(seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("ffmpeg couldn't read that audio: " + (r.stderr or "")[-300:])

# ---- speech to text ----
_stt = {"name": None, "model": None}

def stt_model(name):
    """Lazy-load and keep ONE model resident — two int8 Whisper models in RAM just
    contend for the same 12 cores on the next call."""
    if _stt["name"] != name or _stt["model"] is None:
        from faster_whisper import WhisperModel
        _stt["model"] = None                    # free the previous model first
        _stt["model"] = WhisperModel(name, device="cpu", compute_type="int8")
        _stt["name"] = name
    return _stt["model"]

def transcribe_worker(jid, clip, model_name, lang):
    job = jobs[jid]
    job["status"] = "queued"
    with run_lock:
        try:
            job["status"] = "loading model" if _stt["name"] != model_name else "transcribing"
            m = stt_model(model_name)
            job["status"] = "transcribing"
            segs, info = m.transcribe(clip_path(clip), language=lang, vad_filter=True)
            text = " ".join(s.text.strip() for s in segs).strip()
            save_transcript(clip["id"], text)
            job.update(status="done", text=text, seconds=round(info.duration, 1),
                       clip_id=clip["id"])
        except Exception as e:
            job.update(status="error", error=str(e)[:300])

# ---- text to speech ----
_voices = None

def kokoro_voices():
    global _voices
    if _voices is None:
        try:
            r = subprocess.run([KOKORO, "--list-voices"], capture_output=True, text=True, timeout=180)
            _voices = [v.strip() for v in r.stdout.split() if v.strip()]
        except Exception:
            _voices = []
    return _voices

def speak_worker(jid, engine, text, voice, speed, ref_audio, ref_text, trim, nfe):
    job = jobs[jid]
    job["status"] = "queued"
    name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.wav"
    out  = os.path.join(OUT_DIR, name)
    tmp_ref = tmp_gen = None
    with run_lock:
        try:
            if engine == "kokoro":
                job["status"] = "generating"
                lang = VOICE_LANG.get(voice[:2], "en-us")
                r = subprocess.run([KOKORO, "-o", out, "-v", voice, "-s", str(speed), "-l", lang],
                                   input=respell(text).encode(), capture_output=True, timeout=900)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr.decode(errors="replace") or "kokoro failed")[-300:])
            else:
                ref = ref_audio
                if trim:                       # F5 truncates long references anyway
                    job["status"] = "trimming reference"
                    tmp_ref = os.path.join(tempfile.gettempdir(), f"ref-{uuid.uuid4().hex[:8]}.wav")
                    normalize_audio(ref, tmp_ref, seconds=REF_TRIM_SECONDS)
                    ref = tmp_ref
                # long text via --gen_file, so it can't hit argv limits
                fd, tmp_gen = tempfile.mkstemp(suffix=".txt")
                with os.fdopen(fd, "w") as f: f.write(respell(text))
                job["status"] = ("cloning — about 1 min per sentence on CPU" if nfe <= 16
                                 else "cloning — about 2 min per sentence on CPU")
                r = subprocess.run([F5, "--model", "F5TTS_v1_Base", "-r", ref,
                                    "-s", respell(ref_text), "-f", tmp_gen,
                                    "-o", OUT_DIR, "-w", name,
                                    "--speed", str(speed), "--nfe_step", str(nfe),
                                    "--remove_silence"],
                                   capture_output=True, text=True, timeout=3600)
                if r.returncode != 0 or not os.path.exists(out):
                    raise RuntimeError((r.stderr or r.stdout or "f5-tts failed")[-300:])
            job.update(status="done", url=f"/out/{name}", file=name,
                       seconds=audio_seconds(out))
        except subprocess.TimeoutExpired:
            job.update(status="error", error="Generation timed out.")
        except Exception as e:
            job.update(status="error", error=str(e)[:400])
        finally:
            for p in (tmp_ref, tmp_gen):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass

def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"kind": kind, "status": "queued", "error": None, "text": None,
                 "url": None, "file": None, "seconds": None, "clip_id": None}
    return jid

def safe_path(base, filename):
    """Resolve filename inside base, refusing path traversal."""
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(base, filename))
    if not (path == root or path.startswith(root + os.sep)) or not os.path.isfile(path):
        return None
    return path

# ---- API ----
@app.post("/api/clips")
def api_add_clip():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no file"), 400
    clip_id = uuid.uuid4().hex[:12]
    dest    = os.path.join(CLIPS_DIR, clip_id + ".wav")
    suffix  = os.path.splitext(f.filename)[1][:8] or ".bin"
    tmp     = os.path.join(tempfile.gettempdir(), f"up-{clip_id}{suffix}")
    f.save(tmp)
    try:
        normalize_audio(tmp, dest)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)[:200]), 400
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    source = request.form.get("source") or "uploaded"
    name   = (request.form.get("name") or "").strip()
    if not name:
        name = (time.strftime("Recording %H:%M") if source == "recorded"
                else os.path.basename(f.filename))
    entry = {"id": clip_id, "file": clip_id + ".wav", "name": name, "source": source,
             "seconds": audio_seconds(dest), "created": int(time.time()), "transcript": ""}
    with index_lock:
        items = load_index()
        items.insert(0, entry)          # newest first
        write_index(items)
    entry["url"] = f"/clip/{entry['file']}"
    return jsonify(ok=True, clip=entry)

@app.get("/api/clips")
def api_clips():
    items = load_index()
    for c in items: c["url"] = f"/clip/{c['file']}"
    return jsonify(clips=items)

@app.post("/api/clips/delete")
def api_clip_delete():
    d = request.get_json(force=True, silent=True) or {}
    clip = find_clip(d.get("id") or "")
    if not clip:
        return jsonify(ok=False, msg="unknown clip"), 404
    path = safe_path(CLIPS_DIR, clip["file"])
    if path:
        try: os.remove(path)
        except Exception: pass
    with index_lock:
        write_index([c for c in load_index() if c.get("id") != clip["id"]])
    return jsonify(ok=True)

@app.post("/api/transcribe")
def api_transcribe():
    d = request.get_json(force=True, silent=True) or {}
    clip = find_clip(d.get("clip_id") or "")
    if not clip:
        return jsonify(error="unknown clip"), 404
    if not os.path.exists(clip_path(clip)):
        return jsonify(error="clip file is missing"), 404
    model = d.get("model") if d.get("model") in STT_MODELS else DEFAULT_STT
    lang  = d.get("language") if d.get("language") in LANGS else "en"
    jid = new_job("transcribe")
    threading.Thread(target=transcribe_worker, args=(jid, clip, model, lang), daemon=True).start()
    return jsonify(job_id=jid)

@app.post("/api/speak")
def api_speak():
    d = request.get_json(force=True, silent=True) or {}
    text   = (d.get("text") or "").strip()
    engine = "f5" if d.get("engine") == "f5" else "kokoro"
    if not text:
        return jsonify(error="no text to speak"), 400
    try:
        speed = min(2.0, max(0.5, float(d.get("speed") or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    ref_audio = ref_text = None
    voice, trim = "af_heart", False
    nfe = 16 if str(d.get("nfe")) == "16" else 32
    if engine == "kokoro":
        v = d.get("voice") or "af_heart"
        voice = v if v in kokoro_voices() else "af_heart"
    else:
        preset = find_preset(d.get("preset_id") or "")
        if preset:
            ref_audio = os.path.join(PRESETS_DIR, preset["file"])
            ref_text  = preset["ref_text"]
            trim      = False          # already baked in when the preset was saved
            if not os.path.exists(ref_audio):
                return jsonify(error="that voice preset's audio is missing"), 404
        else:
            clip = find_clip(d.get("clip_id") or "")
            if not clip:
                return jsonify(error="pick a reference clip first"), 400
            if not os.path.exists(clip_path(clip)):
                return jsonify(error="that reference clip is missing"), 404
            ref_audio = clip_path(clip)
            ref_text  = (d.get("ref_text") or "").strip()
            trim      = bool(d.get("trim"))
            # Without a transcript F5-TTS downloads and runs Whisper itself — slow and
            # surprising. Make it an explicit error instead.
            if not ref_text:
                return jsonify(error="tell me what is said in the reference clip"), 400
    jid = new_job("speak")
    threading.Thread(target=speak_worker,
                     args=(jid, engine, text, voice, speed, ref_audio, ref_text, trim, nfe),
                     daemon=True).start()
    return jsonify(job_id=jid)

@app.get("/api/presets")
def api_presets():
    items = load_presets()
    for p in items: p["url"] = f"/preset/{p['file']}"
    return jsonify(presets=items)

@app.post("/api/presets")
def api_preset_save():
    d = request.get_json(force=True, silent=True) or {}
    clip = find_clip(d.get("clip_id") or "")
    if not clip or not os.path.exists(clip_path(clip)):
        return jsonify(ok=False, msg="pick a reference clip first"), 400
    ref_text = (d.get("ref_text") or "").strip()
    if not ref_text:
        return jsonify(ok=False, msg="fill in what is said in the clip first"), 400
    name = (d.get("name") or "").strip() or clip["name"]
    pid  = uuid.uuid4().hex[:12]
    dest = os.path.join(PRESETS_DIR, pid + ".wav")
    try:
        if d.get("trim"):
            normalize_audio(clip_path(clip), dest, seconds=REF_TRIM_SECONDS)
        else:
            shutil.copyfile(clip_path(clip), dest)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)[:200]), 500
    entry = {"id": pid, "file": pid + ".wav", "name": name, "ref_text": ref_text,
             "seconds": audio_seconds(dest), "from": clip["name"], "created": int(time.time())}
    with index_lock:
        items = load_presets()
        items.insert(0, entry)
        write_presets(items)
    entry["url"] = f"/preset/{entry['file']}"
    return jsonify(ok=True, preset=entry)

@app.post("/api/presets/delete")
def api_preset_delete():
    d = request.get_json(force=True, silent=True) or {}
    preset = find_preset(d.get("id") or "")
    if not preset:
        return jsonify(ok=False, msg="unknown preset"), 404
    path = safe_path(PRESETS_DIR, preset["file"])
    if path:
        try: os.remove(path)
        except Exception: pass
    with index_lock:
        write_presets([p for p in load_presets() if p.get("id") != preset["id"]])
    return jsonify(ok=True)

@app.get("/preset/<path:filename>")
def preset_file(filename):
    return send_from_directory(PRESETS_DIR, filename)

@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    return jsonify(job)

@app.get("/api/voices")
def api_voices():
    return jsonify(voices=kokoro_voices(), lang_of=VOICE_LANG)

@app.post("/api/out/delete")
def api_out_delete():
    d = request.get_json(force=True, silent=True) or {}
    path = safe_path(OUT_DIR, d.get("file") or "")
    if not path:
        return jsonify(ok=False, msg="invalid file"), 400
    try:
        os.remove(path)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)[:200]), 500

@app.get("/clip/<path:filename>")
def clip_file(filename):
    return send_from_directory(CLIPS_DIR, filename)

@app.get("/out/<path:filename>")
def out_file(filename):
    return send_from_directory(OUT_DIR, filename)

@app.get("/icon.png")
@app.get("/favicon.ico")
def icon():
    return send_from_directory(HERE, "icon.png", mimetype="image/png")

@app.get("/")
def index():
    # re-read each time + no-store so home-screen shortcuts always get the latest UI
    page = open(os.path.join(HERE, "index.html")).read()
    r = Response(page, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
