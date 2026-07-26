"""Recorded and uploaded audio, their transcripts, and the voice presets built from them."""
import json, os, shutil, tempfile, time, uuid

from flask import jsonify, request, send_from_directory

from core import HERE, app, index_lock, safe_path, write_json
from media import audio_seconds, normalize_audio

CLIPS_DIR    = os.path.join(HERE, "clips")
PRESETS_DIR  = os.path.join(HERE, "presets")
INDEX_FILE   = os.path.join(HERE, "clips.json")
PRESETS_FILE = os.path.join(HERE, "presets.json")
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(PRESETS_DIR, exist_ok=True)

REF_TRIM_SECONDS = 10   # F5 gains nothing from a longer reference

# ---- clip index (clips.json) ----
def load_index():
    try:
        with open(INDEX_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_index(items):
    write_json(INDEX_FILE, items)

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
    write_json(PRESETS_FILE, items)

def find_preset(preset_id):
    return next((p for p in load_presets() if p.get("id") == preset_id), None)


def save_transcript(clip_id, text):
    with index_lock:
        items = load_index()
        for c in items:
            if c.get("id") == clip_id: c["transcript"] = text
        write_index(items)


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


@app.get("/clip/<path:filename>")
def clip_file(filename):
    return send_from_directory(CLIPS_DIR, filename)
