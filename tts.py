"""The two speech engines, kept resident, and everything that asks them to say something.

Kokoro is the English engine and Piper the Dutch one, each driven by its own interpreter so
their dependencies stay in their own venvs. F5 is the voice-cloning path and still runs as a
CLI call.
"""
import hashlib, json, os, select, subprocess, tempfile, threading, time, uuid

from flask import jsonify, request, send_from_directory

from clips import PRESETS_DIR, REF_TRIM_SECONDS, clip_path, find_clip, find_preset
from core import HERE, app, jobs, new_job, run_lock, safe_path
from media import audio_seconds, normalize_audio
from textprep import respell, speech_text

OUT_DIR     = os.path.join(HERE, "outputs")
SAMPLES_DIR = os.path.join(HERE, "samples")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(SAMPLES_DIR, exist_ok=True)

F5     = os.path.expanduser("~/.local/bin/f5-tts")
# Kokoro and Piper both run as resident workers instead of per-render CLI calls, each driven
# by its OWN interpreter so their dependencies stay in their own venvs. Kokoro is the English
# engine (54 voices, no Dutch); Piper is the Dutch one.
KOKORO_DIR   = os.path.expanduser("~/.local/share/kokoro-tts")
PIPER_DIR    = os.path.expanduser("~/.local/share/piper-tts")
PIPER_VOICES = os.path.join(PIPER_DIR, "voices")
ENGINES = {
    "kokoro": {"py": os.path.join(KOKORO_DIR, "venv/bin/python"),
               "worker": os.path.join(HERE, "kokoro_worker.py"), "arg": KOKORO_DIR},
    "piper":  {"py": os.path.join(PIPER_DIR, "venv/bin/python"),
               "worker": os.path.join(HERE, "piper_worker.py"),  "arg": PIPER_VOICES},
}


# Kokoro voice prefix -> language code. Without this a British voice is phonemized
# with US rules and sounds wrong.
VOICE_LANG = {"af":"en-us", "am":"en-us", "bf":"en-gb", "bm":"en-gb", "jf":"ja", "jm":"ja",
              "zf":"cmn", "zm":"cmn", "ef":"es", "em":"es", "ff":"fr-fr",
              "hf":"hi", "hm":"hi", "if":"it", "im":"it", "pf":"pt-br", "pm":"pt-br"}

# What the voice-preview button says, in the language the engine is for.
SAMPLE_TEXT = {"kokoro": "Hello! How can I assist you today?",
               "piper":  "Hallo! Hoe kan ik je vandaag helpen?"}


NFE_STEPS        = (16, 32)   # sampling steps: 16 is ~2x faster, 32 is the default quality

# ---- text to speech ----
# Both engines are kept resident in a worker process. Loading Kokoro's ONNX model cost ~1.9 s
# on every single render when we shelled out to the CLI — more than the audio itself for a
# short sentence. Now that's paid once per engine, on its first request after a restart.
_workers      = {name: {"proc": None, "used": 0.0} for name in ENGINES}
# Held across a whole request/response, not just startup. The idle reaper takes the same lock,
# which is what stops it from killing a worker in the gap between starting one and writing
# to it. Renders are already serialized by run_lock, so this adds no real contention.
_worker_lock  = threading.RLock()
TTS_START_TIMEOUT = 180           # first load reads a few hundred MB of model off disk
TTS_CALL_TIMEOUT  = 900
# An idle Kokoro worker holds 550 MB-1 GB (onnxruntime keeps its arena) and restarting costs
# ~1.2 s, so it's cheap to let go. 0 disables the reaper and keeps them resident forever.
TTS_IDLE_SECONDS  = int(os.environ.get("KOKORO_IDLE_MINUTES", "10")) * 60

def _worker_line(engine, proc, timeout):
    """Read one newline-terminated response, without blocking forever on a wedged worker.
    The protocol is strictly one request → one response, so there's never a second line
    buffered behind the one we want."""
    buf, end, fd = b"", time.monotonic() + timeout, proc.stdout.fileno()
    while b"\n" not in buf:
        remain = end - time.monotonic()
        if remain <= 0:
            raise TimeoutError(f"the {engine} worker stopped responding")
        if not select.select([fd], [], [], min(remain, 1.0))[0]:
            if proc.poll() is not None:
                raise RuntimeError(f"the {engine} worker exited")
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            raise RuntimeError(f"the {engine} worker closed its pipe")
        buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode())

def worker_start(engine):
    """Start an engine's worker if it isn't running. A dead worker is simply replaced — the
    next render pays the load again rather than failing for good."""
    spec = ENGINES[engine]
    with _worker_lock:
        proc = _workers[engine]["proc"]
        if proc is not None and proc.poll() is None:
            return proc
        if not os.path.exists(spec["py"]):
            raise RuntimeError(f"{engine}'s interpreter is missing at {spec['py']}")
        proc = subprocess.Popen([spec["py"], spec["worker"], spec["arg"]],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        hello = _worker_line(engine, proc, TTS_START_TIMEOUT)   # blocks until the model loads
        if not hello.get("ok"):
            proc.kill()
            raise RuntimeError(hello.get("error") or f"the {engine} worker failed to start")
        _workers[engine]["proc"] = proc
        return proc

def worker_call(engine, payload, timeout=TTS_CALL_TIMEOUT):
    with _worker_lock:
        proc = worker_start(engine)
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode())
            proc.stdin.flush()
            res = _worker_line(engine, proc, timeout)
        except Exception:
            # a broken pipe or a timeout means this worker is no good; drop it so the next
            # call starts a fresh one
            worker_stop(engine, proc)
            raise
        finally:
            _workers[engine]["used"] = time.monotonic()
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or f"{engine} failed")
    return res

def worker_stop(engine, proc=None):
    """Shut a worker down and let its memory go. Closing stdin ends the worker's read loop;
    the kill is only for one that has stopped listening."""
    with _worker_lock:
        proc = proc or _workers[engine]["proc"]
        if proc is None:
            return
        if _workers[engine]["proc"] is proc:
            _workers[engine]["proc"] = None
        try:
            if proc.stdin and not proc.stdin.closed: proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass

def worker_reaper():
    """Release the models after a stretch of no renders. Takes the same lock as worker_call,
    so it can never land in the middle of one."""
    while True:
        time.sleep(30)
        try:
            with _worker_lock:
                for engine, state in _workers.items():
                    proc = state["proc"]
                    if (proc is not None and proc.poll() is None
                            and time.monotonic() - state["used"] > TTS_IDLE_SECONDS):
                        worker_stop(engine, proc)
        except Exception:
            pass          # a reaper that dies would leak the workers for the process lifetime

_voices = {}

def kokoro_voices():
    if "kokoro" not in _voices:
        try:
            _voices["kokoro"] = worker_call("kokoro", {"op": "voices"},
                                            timeout=TTS_START_TIMEOUT)["voices"]
        except Exception:
            _voices["kokoro"] = []
    return _voices["kokoro"]

def piper_voices():
    """[{id, lang, name, quality}] for whatever Dutch models are in the voices dir."""
    if "piper" not in _voices:
        try:
            _voices["piper"] = worker_call("piper", {"op": "voices"},
                                           timeout=TTS_START_TIMEOUT)["voices"]
        except Exception:
            _voices["piper"] = []
    return _voices["piper"]

def piper_voice_ids():
    return [v["id"] for v in piper_voices()]

def tts_engine_of(voice):
    """Which engine owns a voice name. Kokoro's are like 'af_heart', Piper's 'nl_NL-ronnie-
    medium', so the name alone is enough and callers don't have to track it."""
    if voice in piper_voice_ids():
        return "piper"
    if voice in kokoro_voices():
        return "kokoro"
    return None

def tts_say(voice, text, speed, out):
    """Render with whichever engine owns the voice."""
    engine = tts_engine_of(voice)
    if engine is None:
        raise RuntimeError(f"unknown voice: {voice}")
    payload = {"op": "say", "text": text, "voice": voice, "speed": speed, "out": out}
    if engine == "kokoro":
        payload["lang"] = VOICE_LANG.get(voice[:2], "en-us")
    return worker_call(engine, payload)

def speak_worker(jid, engine, text, voice, speed, ref_audio, ref_text, trim, nfe):
    job = jobs[jid]
    job["status"] = "queued"
    name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.wav"
    out  = os.path.join(OUT_DIR, name)
    tmp_ref = tmp_gen = None
    with run_lock:
        try:
            if engine != "f5":
                job["status"] = "generating"
                tts_say(voice, respell(text), speed, out)
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


@app.post("/api/speak")
def api_speak():
    d = request.get_json(force=True, silent=True) or {}
    text   = (d.get("text") or "").strip()
    engine = "f5" if d.get("engine") == "f5" else "kokoro"
    # The chat panel sends the reply verbatim and asks for the markdown to be taken out here,
    # so there's one implementation of that rather than one per caller.
    if d.get("strip"):
        text = speech_text(text)
    if not text:
        return jsonify(error="no text to speak"), 400
    try:
        speed = min(2.0, max(0.5, float(d.get("speed") or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    ref_audio = ref_text = None
    voice, trim = "af_heart", False
    nfe = 16 if str(d.get("nfe")) == "16" else 32
    if engine != "f5":
        # Kokoro and Piper are both picked by voice name, so the caller doesn't have to say
        # which engine a voice belongs to — tts_say works that out.
        v = d.get("voice") or "af_heart"
        voice = v if tts_engine_of(v) else "af_heart"
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


@app.get("/api/voices")
def api_voices():
    return jsonify(voices=kokoro_voices(), piper=piper_voices(), lang_of=VOICE_LANG)

@app.get("/api/sample/<voice>")
def api_sample(voice):
    """A short spoken sample of one voice, for the ▶ button beside the pickers. Rendered on
    first request and then served from disk, so browsing voices costs ~1 s once each."""
    engine = tts_engine_of(voice)
    if engine is None:
        return jsonify(error="unknown voice"), 404
    name = voice + ".wav"
    path = os.path.join(SAMPLES_DIR, name)
    if not os.path.exists(path):
        # Don't sit behind a two-minute F5 clone: fail with something the page can explain.
        if not run_lock.acquire(timeout=30):
            return jsonify(error="busy generating something else — try again in a moment"), 503
        try:
            tts_say(voice, SAMPLE_TEXT[engine], 1.0, path)
        except Exception as e:
            if os.path.exists(path): os.remove(path)     # don't cache a half-written file
            return jsonify(error=str(e)[:200]), 500
        finally:
            run_lock.release()
    return send_from_directory(SAMPLES_DIR, name)

@app.get("/api/say")
def api_say():
    """One short phrase, spoken and returned as audio in the same request.

    For hearing a respelling before committing a book to it: you type a word, tap ▶, and know
    in a second whether the engine says it right. /api/speak is the wrong shape for that — it's
    a job to poll, and awaiting a poll loses the tap iOS needs to play audio at all, which is
    why the samples beside the voice pickers are a plain GET too.

    Whatever is asked for is respelled on the way in, since what you want to hear is what a
    render would say, not what you typed."""
    text = (request.args.get("text") or "").strip()[:200]
    voice = request.args.get("voice") or ""
    if not text:
        return jsonify(error="nothing to say"), 400
    if tts_engine_of(voice) is None:
        return jsonify(error="unknown voice"), 404
    spoken = respell(text)
    # Cached under a name derived from what was actually spoken, so tapping ▶ twice — or a
    # second book asking for the same word in the same voice — costs nothing.
    name = "say-%s.wav" % hashlib.sha1(f"{voice}\n{spoken}".encode()).hexdigest()[:16]
    path = os.path.join(SAMPLES_DIR, name)
    if not os.path.exists(path):
        # Don't sit behind a two-minute F5 clone: fail with something the page can explain.
        if not run_lock.acquire(timeout=30):
            return jsonify(error="busy generating something else — try again in a moment"), 503
        try:
            tts_say(voice, spoken, 1.0, path)
        except Exception as e:
            if os.path.exists(path): os.remove(path)     # don't cache a half-written file
            return jsonify(error=str(e)[:200]), 500
        finally:
            run_lock.release()
    return send_from_directory(SAMPLES_DIR, name)

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


@app.get("/out/<path:filename>")
def out_file(filename):
    return send_from_directory(OUT_DIR, filename)
