"""Everything the feature modules share: the Flask app, the job table, the locks that
keep one model on the GPU at a time, and the two file helpers.

Kept deliberately thin. It exists so books.py and chat.py can both reach the app without
importing each other, not as a home for anything that didn't fit elsewhere.
"""
import json, logging, os, threading, uuid

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SPEECH_PORT", "8600"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB — long recordings are fine

LANGS = ("en", "nl")     # what the whisper and chat endpoints will accept

run_lock   = threading.Lock()   # one model at a time: F5-TTS saturates all 12 cores
index_lock = threading.Lock()
jobs       = {}                 # job_id -> state dict (polled by /api/status)

# Its own handler rather than basicConfig: werkzeug's access lines already carry a timestamp
# in the message, and configuring the root logger would print a second one in front of them.
log = logging.getLogger("speech")
if not log.handlers:
    _h = logging.StreamHandler()      # stderr, which is where speech.log comes from
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%d/%b/%Y %H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def log_transfer(r, what):
    """Wrap a file response so the log says who fetched it and how much they took.

    An abandoned download is indistinguishable from a finished one in the access log — both
    are a 200 with no size — and iOS silently discards a file it can't hand anywhere. Counting
    the bytes on the way out separates "the phone never asked for it" from "the phone read all
    26 MB and threw them away", which want opposite fixes. The User-Agent comes along because
    every request arrives from 127.0.0.1 through the tailnet proxy, so it's the only way to
    tell the phone from the PC.
    """
    ua = (request.headers.get("User-Agent") or "?")[:150]
    rng = request.headers.get("Range") or "-"
    total = r.content_length
    body = r.response

    def counted():
        sent = 0
        try:
            for chunk in body:
                sent += len(chunk)
                yield chunk
        finally:
            # Replacing r.response takes the file wrapper out of werkzeug's hands, so closing
            # the file is now this generator's job — on the abandoned transfers as much as the
            # finished ones, which is what the GeneratorExit path is.
            if hasattr(body, "close"):
                body.close()
            log.info("%s: sent %s of %s bytes%s · range=%s · ua=%s", what, sent, total,
                     "" if total in (None, sent) else " — INCOMPLETE", rng, ua)

    r.response = counted()
    return r


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
