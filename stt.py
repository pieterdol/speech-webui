"""Whisper. One model resident at a time, loaded on first use."""
import os, tempfile, threading, uuid

from flask import jsonify, request

from clips import clip_path, find_clip, save_transcript
from core import LANGS, app, jobs, new_job, run_lock
from media import normalize_audio

STT_MODELS  = ("small", "large-v3-turbo")
DEFAULT_STT = "small"

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

def run_stt(job, path, model_name, lang):
    """Shared by the clip path and chat dictation: load the model, transcribe, return text."""
    job["status"] = "loading model" if _stt["name"] != model_name else "transcribing"
    m = stt_model(model_name)
    job["status"] = "transcribing"
    segs, info = m.transcribe(path, language=lang, vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip(), round(info.duration, 1)

def dictate_worker(jid, path, model_name, lang):
    """A spoken chat turn. The words are the point; the audio is scratch, so it never enters
    the clip index and the temp file goes away as soon as it's been read."""
    job = jobs[jid]
    job["status"] = "queued"
    with run_lock:
        try:
            text, seconds = run_stt(job, path, model_name, lang)
            job.update(status="done", text=text, seconds=seconds)
        except Exception as e:
            job.update(status="error", error=str(e)[:300])
        finally:
            if os.path.exists(path):
                try: os.remove(path)
                except Exception: pass

def transcribe_worker(jid, clip, model_name, lang):
    job = jobs[jid]
    job["status"] = "queued"
    with run_lock:
        try:
            text, seconds = run_stt(job, clip_path(clip), model_name, lang)
            save_transcript(clip["id"], text)
            job.update(status="done", text=text, seconds=seconds, clip_id=clip["id"])
        except Exception as e:
            job.update(status="error", error=str(e)[:300])


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


@app.post("/api/dictate")
def api_dictate():
    """Speech → text without keeping the audio. Same Whisper path as /api/transcribe, but the
    recording never reaches clips/ or clips.json — a spoken chat turn isn't a clip."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no audio"), 400
    model = request.form.get("model") if request.form.get("model") in STT_MODELS else DEFAULT_STT
    lang  = request.form.get("language") if request.form.get("language") in LANGS else "en"
    tag    = uuid.uuid4().hex[:8]
    suffix = os.path.splitext(f.filename)[1][:8] or ".bin"
    # "-in" in the upload's name: without it a .wav upload would resolve to the same path as
    # the normalized output, and ffmpeg refuses to write its own input.
    raw    = os.path.join(tempfile.gettempdir(), f"dict-{tag}-in{suffix}")
    wav    = os.path.join(tempfile.gettempdir(), f"dict-{tag}.wav")
    f.save(raw)
    try:
        normalize_audio(raw, wav)
    except Exception as e:
        return jsonify(error=str(e)[:200]), 400
    finally:
        if os.path.exists(raw): os.remove(raw)
    jid = new_job("dictate")
    threading.Thread(target=dictate_worker, args=(jid, wav, model, lang), daemon=True).start()
    return jsonify(job_id=jid)
