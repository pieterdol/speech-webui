"""Everything the feature modules share: the Flask app, the job table, the locks that
keep one model on the GPU at a time, and the two file helpers.

Kept deliberately thin. It exists so books.py and chat.py can both reach the app without
importing each other, not as a home for anything that didn't fit elsewhere.
"""
import json, os, threading, uuid

from flask import Flask, jsonify

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SPEECH_PORT", "8600"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB — long recordings are fine

LANGS = ("en", "nl")     # what the whisper and chat endpoints will accept

run_lock   = threading.Lock()   # one model at a time: F5-TTS saturates all 12 cores
index_lock = threading.Lock()
jobs       = {}                 # job_id -> state dict (polled by /api/status)

def write_json(path, data):
    """Write via a temp file and rename, so a reader never sees a half-written index.

    Not theoretical: a whole-book render rewrites books.json after every chapter while the
    page polls it, and a plain truncate-and-write made the book disappear mid-save."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"kind": kind, "status": "queued", "error": None, "text": None,
                 "url": None, "file": None, "seconds": None, "clip_id": None,
                 # chat with speech: audio arrives sentence by sentence while the reply is
                 # still being written, so the page plays it before the text is finished
                 "audio": [], "audio_done": False, "audio_error": None}
    return jid

def safe_path(base, filename):
    """Resolve filename inside base, refusing path traversal."""
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(base, filename))
    if not (path == root or path.startswith(root + os.sep)) or not os.path.isfile(path):
        return None
    return path


@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    return jsonify(job)
