#!/usr/bin/env python3
"""
speech-webui — a phone-friendly web front-end for the local speech tools.

  speech → text   faster-whisper (in-process, model kept resident)
  text → speech   Kokoro (54 built-in voices, seconds) or F5-TTS (clones a clip, minutes)
  chat            Ollama (Qwen et al. on the GPU), with replies spoken by Kokoro

Both TTS engines keep their own venvs so this app doesn't duplicate their dependencies.
F5-TTS is invoked as a CLI per render (it's minutes of work anyway); Kokoro instead runs
as a resident worker started with ITS interpreter — see kokoro_worker.py — because the
per-invocation model load was costing more than the audio on short text. Ollama is a
separate server we talk to over HTTP on :11434.

Reach it from your phone over Tailscale at https://your-machine.your-tailnet.ts.net:8443
(HTTPS is required — Safari blocks the microphone and the clipboard on plain http).
"""
import json, os, queue, re, select, shutil, subprocess, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from flask import Flask, Response, jsonify, request, send_from_directory

import epub

HERE         = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR    = os.path.join(HERE, "clips")
OUT_DIR      = os.path.join(HERE, "outputs")
PRESETS_DIR  = os.path.join(HERE, "presets")
SAMPLES_DIR  = os.path.join(HERE, "samples")
BOOKS_DIR    = os.path.join(HERE, "books")
INDEX_FILE   = os.path.join(HERE, "clips.json")
PRESETS_FILE = os.path.join(HERE, "presets.json")
CHATS_FILE   = os.path.join(HERE, "chats.json")
BOOKS_FILE   = os.path.join(HERE, "books.json")
PORT         = int(os.environ.get("SPEECH_PORT", "8600"))

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
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(PRESETS_DIR, exist_ok=True)
os.makedirs(SAMPLES_DIR, exist_ok=True)
os.makedirs(BOOKS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB — long recordings are fine

run_lock   = threading.Lock()   # one model at a time: F5-TTS saturates all 12 cores
index_lock = threading.Lock()
jobs       = {}                 # job_id -> state dict (polled by /api/status)

# Deliberately NOT run_lock: Ollama offloads the whole model to the Radeon, so a chat
# and a Kokoro render use different hardware and should overlap. It's still a lock —
# two chats at once would just thrash one GPU (OLLAMA_NUM_PARALLEL is 1 anyway).
chat_lock = threading.Lock()

STT_MODELS  = ("small", "large-v3-turbo")
DEFAULT_STT = "small"
LANGS       = ("en", "nl")

# Chat defaults. A per-request keep_alive overrides the server's OLLAMA_KEEP_ALIVE, so this
# value — not the systemd unit's — is what actually governs chats from this app. 5 m matches
# Ollama's default: on the Vulkan backend a cold load is ~4 s, so squatting on 5.2 GB of a
# 16 GB card for any longer costs more than it saves when ComfyUI wants the VRAM back.
DEFAULT_CHAT_MODEL = "qwen3:8b"
CHAT_KEEP_ALIVE    = "5m"
CHAT_NUM_CTX       = 8192
# Per-read timeout, not a total. Deliberately longer than Ollama's own OLLAMA_LOAD_TIMEOUT
# (5 min): at exactly 300 s this client was the one hanging up, and the log then blamed the
# "client connection closed" instead of reporting why the load was actually slow.
CHAT_LOAD_TIMEOUT  = 600
# Replies get read out loud, so the default persona is tuned for listening, not skimming.
DEFAULT_SYSTEM = ("You are a helpful assistant whose answers are read out loud by a "
                  "text-to-speech voice. Keep replies short and conversational — a few "
                  "sentences unless asked for more. Write plain prose: no markdown, no "
                  "bullet lists, no code blocks unless explicitly asked for code.")
# Appended when the chat is set to Dutch. Kept out of the editable system prompt so switching
# language doesn't rewrite whatever persona is in there.
LANG_NOTE = {
    "nl": ("The user speaks and writes Dutch. Understand Dutch input fully, but always write "
           "your reply in English, whatever language the question was asked in. Never answer "
           "in Dutch."),
}
# The system prompt alone doesn't hold: qwen3:8b mirrors the language of the latest user turn
# and answers in Dutch anyway, especially on Dutch subject matter. Two things that do hold,
# both attached to what gets SENT and never to what's stored in chats.json:
#   - a reminder on the turn itself, where the model is actually looking
#   - one fabricated exchange showing the pattern, which steers language far harder than any
#     instruction does. It also matters that turn one comes out right: once a Dutch reply is
#     in the history, every later turn copies it.
LANG_REMINDER = {"nl": "\n\n[Reply in English.]"}
# Two examples, not one: the failure mode is specifically a Dutch question about Dutch
# subject matter, where the model slips back into Dutch. The second example is exactly that.
LANG_PRIMER = {
    "nl": [{"role": "user", "content": "Hoe gaat het met je?"},
           {"role": "assistant",
            "content": "I'm doing well, thanks for asking! What can I help you with?"},
           {"role": "user", "content": "Hoeveel inwoners heeft Rotterdam ongeveer?"},
           {"role": "assistant",
            "content": "Rotterdam has roughly 650,000 inhabitants, making it the "
                       "second-largest city in the Netherlands."}],
}

# Kokoro voice prefix -> language code. Without this a British voice is phonemized
# with US rules and sounds wrong.
VOICE_LANG = {"af":"en-us", "am":"en-us", "bf":"en-gb", "bm":"en-gb", "jf":"ja", "jm":"ja",
              "zf":"cmn", "zm":"cmn", "ef":"es", "em":"es", "ff":"fr-fr",
              "hf":"hi", "hm":"hi", "if":"it", "im":"it", "pf":"pt-br", "pm":"pt-br"}

# What the voice-preview button says, in the language the engine is for.
SAMPLE_TEXT = {"kokoro": "Hello! How can I assist you today?",
               "piper":  "Hallo! Hoe kan ik je vandaag helpen?"}

# Neither engine has a pronunciation-override syntax, so hard words are respelled
# phonetically on the way in.
RESPELL = {"Pieter": "Peter"}

REF_TRIM_SECONDS = 10   # F5 gains nothing from a longer reference
NFE_STEPS        = (16, 32)   # sampling steps: 16 is ~2x faster, 32 is the default quality

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

def respell(text):
    for src, dst in RESPELL.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
    return text

# ---- turning a written reply into something worth listening to ----
# Kokoro reads punctuation literally, so "**important**" becomes "asterisk asterisk important"
# and a URL becomes a spelled-out mess. Lives here rather than in the browser so the streamed
# and the manual "speak this reply" paths can't drift apart.
_MD = [
    (re.compile(r"```.*?```", re.S), " "),          # code blocks are unlistenable
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links: keep the words, drop the URL
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),
    (re.compile(r"(\*\*|__|~~|\*|_)"), ""),
    (re.compile(r"\n{2,}"), "\n"),
]

def speech_text(text):
    for pattern, repl in _MD:
        text = pattern.sub(repl, text or "")
    return text.strip()

# Sentence boundary: closing punctuation, optional quote/bracket, then whitespace. A decimal
# ("3.5") has no space after the dot, so it never matches.
_BOUNDARY = re.compile(r'(?<=[.!?…])["\'”’)\]]*(\s+)')
# Periods that end an abbreviation rather than a sentence.
_ABBREV = {"e.g", "i.e", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
           "fig", "approx", "no", "al", "inc", "ltd"}

def _is_real_end(text, dot):
    """dot = index just past the sentence-ending punctuation."""
    if text[dot - 1] != ".":
        return True                      # ! and ? don't have this problem
    word = re.search(r"([A-Za-z.]+)\.$", text[:dot])
    if not word:
        return True
    w = word.group(1).rstrip(".").lower()
    return not (w in _ABBREV or len(w) == 1)   # "J. Smith" shouldn't split either

def cut_sentences(buf, min_chars, flush=False):
    """Split off whole sentences worth speaking, and return (chunks, remainder).

    Chunks are held to a minimum length because each render costs ~0.3 s fixed: below roughly
    half a second of audio, generating the next chunk takes longer than playing the current
    one and the speech develops gaps. On flush, whatever is left goes out regardless."""
    # An unclosed code fence means more of it is still streaming in — wait rather than read
    # half a fence out loud.
    if not flush and buf.count("```") % 2:
        return [], buf
    chunks, start = [], 0
    for m in _BOUNDARY.finditer(buf):
        dot = m.start()          # just past the . ! or ?
        end = m.start(1)         # …and past any closing quote or bracket, which belongs to
                                 # this sentence, not to the gap between sentences
        if not _is_real_end(buf, dot):
            continue
        if end - start < min_chars:
            continue                                # too short: let it grow into the next one
        chunks.append(buf[start:end].strip())
        start = m.end()
    remainder = buf[start:]
    if flush:                       # flush means nothing is held back, whitespace included
        if remainder.strip():
            chunks.append(remainder.strip())
        remainder = ""
    return [c for c in chunks if c], remainder

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

# ---- chats (chats.json) ----
# Conversations live server-side, not in localStorage, so one started on the PC can be
# picked up on the phone. Messages are stored inline — they're a few KB of text.
def load_chats():
    try:
        with open(CHATS_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_chats(items):
    write_json(CHATS_FILE, items)

def find_chat(chat_id):
    return next((c for c in load_chats() if c.get("id") == chat_id), None)

def chat_summary(chat):
    """The dropdown only needs a label, so don't ship the whole transcript to build it."""
    msgs = chat.get("messages") or []
    last = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
    return {k: chat.get(k) for k in ("id", "name", "model", "system", "language",
                                     "created", "updated")} | \
           {"count": len(msgs), "last": last[:80]}

def append_turn(chat_id, user_text, reply, model):
    with index_lock:
        items = load_chats()
        for c in items:
            if c.get("id") != chat_id: continue
            c.setdefault("messages", [])
            c["messages"].append({"role": "user", "content": user_text, "ts": int(time.time())})
            c["messages"].append({"role": "assistant", "content": reply, "ts": int(time.time()),
                                  "model": model})
            c["model"]   = model
            c["updated"] = int(time.time())
            # "New chat" is a placeholder — name it after whatever it turned out to be about
            if not (c.get("name") or "").strip() or c["name"] == "New chat":
                c["name"] = (user_text.strip().splitlines() or [""])[0][:48] or "New chat"
        write_chats(items)

# ---- ollama ----
THINK_TAGS = re.compile(r"<think>.*?</think>\s*", re.S)
_caps = {}      # model -> its Ollama capability list, e.g. ["completion", "vision"]

def ollama_json(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(OLLAMA + path, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def ollama_error(e):
    """Turn a connection failure into the one instruction that actually fixes it."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return (json.loads(e.read().decode()).get("error") or str(e))[:300]
        except Exception:
            return f"Ollama returned HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"Ollama isn't reachable at {OLLAMA} — start it with: ollama serve"
    return str(e)[:300]

def ollama_models():
    tags = ollama_json("/api/tags")
    out  = []
    for m in tags.get("models") or []:
        d = m.get("details") or {}
        name = m.get("name")
        # Vision models answer text fine — they're just tuned for images they can't be given
        # here, and trade some text ability for it. Worth flagging in the picker.
        out.append({"name": name, "size": m.get("size"),
                    "params": d.get("parameter_size"), "quant": d.get("quantization_level"),
                    "vision": "vision" in model_caps(name)})
    # Qwen first (that's what this box has), then the rest, each alphabetically
    out.sort(key=lambda m: (not (m["name"] or "").startswith("qwen"), m["name"] or ""))
    return out

def model_caps(model):
    """Ollama's capability list for a model, fetched once and kept."""
    if model not in _caps:
        try:
            _caps[model] = ollama_json("/api/show", {"model": model}).get("capabilities") or []
        except Exception:
            _caps[model] = []
    return _caps[model]

def model_thinks(model):
    """qwen3 reasons out loud unless told not to; qwen2.5-coder rejects the flag entirely.
    Ask before sending it."""
    return "thinking" in model_caps(model)

def ollama_chat_stream(model, messages):
    body = {"model": model, "messages": messages, "stream": True,
            "keep_alive": CHAT_KEEP_ALIVE, "options": {"num_ctx": CHAT_NUM_CTX}}
    if model_thinks(model):
        body["think"] = False      # don't spend GPU time on reasoning nobody will hear
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=CHAT_LOAD_TIMEOUT) as r:
        for line in r:                     # newline-delimited JSON, one object per chunk
            line = line.strip()
            if not line: continue
            d = json.loads(line)
            if d.get("error"): raise RuntimeError(d["error"])
            chunk = (d.get("message") or {}).get("content") or ""
            if chunk: yield chunk
            if d.get("done"): break

SPEAK_MIN_CHARS   = 45    # ≈3 s of speech; comfortably above the ~0.5 s break-even
# The first chunk sets how long you sit in silence, so let it be a single short sentence.
# There's no catching-up risk: nothing is playing yet for it to fall behind.
SPEAK_FIRST_CHARS = 20

def speak_stream_worker(job, q, voice, speed):
    """Renders queued sentences in order while the model is still writing later ones. Runs on
    its own thread so a Kokoro render never stalls reading the token stream."""
    while True:
        text = q.get()
        if text is None:
            break
        try:
            name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.wav"
            out  = os.path.join(OUT_DIR, name)
            with run_lock:          # shares the CPU engines' lock; Kokoro is quick, F5 is not
                res = tts_say(voice, respell(text), speed, out)
            job["audio"].append({"url": f"/out/{name}", "file": name,
                                 "seconds": res.get("seconds")})
        except Exception as e:
            # one bad sentence shouldn't silence the rest of the reply
            job["audio_error"] = str(e)[:200]
    job["audio_done"] = True

def chat_system(chat, english_only):
    """The chat's own persona, plus the answer-in-English instruction when it applies."""
    parts = [p for p in (chat.get("system"), LANG_NOTE["nl"] if english_only else None) if p]
    return "\n\n".join(parts)

def chat_worker(jid, chat_id, user_text, model, voice=None, speed=1.0, english_only=False):
    job = jobs[jid]
    job["status"] = "queued"
    q = speaker = None
    with chat_lock:
        try:
            chat = find_chat(chat_id)
            if not chat:
                raise RuntimeError("that chat no longer exists")
            system = chat_system(chat, english_only)
            msgs = ([{"role": "system", "content": system}] if system else []) \
                 + (LANG_PRIMER["nl"] if english_only else []) \
                 + [{"role": m["role"], "content": m["content"]} for m in chat.get("messages") or []] \
                 + [{"role": "user",
                     "content": user_text + (LANG_REMINDER["nl"] if english_only else "")}]
            if voice:
                q = queue.Queue()
                speaker = threading.Thread(target=speak_stream_worker,
                                           args=(job, q, voice, speed), daemon=True)
                speaker.start()
            job["status"] = "loading model"
            acc, pending, spoken_any = [], "", False
            for chunk in ollama_chat_stream(model, msgs):
                acc.append(chunk)
                job["text"]   = "".join(acc)      # /api/status streams this back as it grows
                job["status"] = "writing"
                if q:
                    pending += chunk
                    ready, pending = cut_sentences(
                        pending, SPEAK_FIRST_CHARS if not spoken_any else SPEAK_MIN_CHARS)
                    for sentence in ready:
                        said = speech_text(THINK_TAGS.sub("", sentence))
                        if said:
                            q.put(said)
                            spoken_any = True
            reply = THINK_TAGS.sub("", "".join(acc)).strip()
            if not reply:
                raise RuntimeError("the model returned an empty reply")
            if q:
                for sentence in cut_sentences(pending, 0, flush=True)[0]:
                    said = speech_text(THINK_TAGS.sub("", sentence))
                    if said: q.put(said)
            append_turn(chat_id, user_text, reply, model)
            job.update(status="done", text=reply)
        except Exception as e:
            job.update(status="error", error=ollama_error(e))
        finally:
            if q:
                q.put(None)           # let the speaker drain and mark audio_done
            else:
                job["audio_done"] = True

# ---- books (books.json + books/<id>/) ----
# A book is a lot of text and a lot of audio, so books.json holds only the index: chapter
# names, word counts, which segments have been rendered, and where you'd got to. The prose
# lives in books/<id>/text/ and the audio in books/<id>/audio/.
#
# Rendering is per chapter, on demand, because the whole of The Institute is 8.4 hours of
# work — you'd never wait for that. Chapters are cut into ~10 minute segments so the first
# audio arrives in a few minutes rather than sixteen, which is safe now that iOS is confirmed
# to advance between files with the screen locked.
SEGMENT_CHARS = 8000      # ≈10 min of speech at the measured ~13.6 characters per second
CHUNK_CHARS   = 600       # one Kokoro/Piper call ≈45 s of audio ≈17 s of work
render_lock   = threading.Lock()      # one book render at a time

def load_books():
    try:
        with open(BOOKS_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_books(items):
    write_json(BOOKS_FILE, items)

def find_book(book_id):
    return next((b for b in load_books() if b.get("id") == book_id), None)

def book_dir(book_id, *parts):
    return os.path.join(BOOKS_DIR, book_id, *parts)

def book_summary(b):
    """Enough for the library list without shipping every chapter."""
    chapters = b.get("chapters") or []
    ready = sum(1 for c in chapters if c.get("state") == "ready")
    return {k: b.get(k) for k in ("id", "title", "author", "language", "voice",
                                  "added", "position", "cover")} | \
           {"chapters": len(chapters), "ready": ready,
            "words": sum(c.get("words", 0) for c in chapters)}

def update_book(book_id, fn):
    """Read-modify-write one book under the index lock. Renders mutate chapter state from a
    worker thread while the page is reading, so this is never done in place."""
    with index_lock:
        items = load_books()
        for b in items:
            if b.get("id") == book_id:
                fn(b)
                b["updated"] = int(time.time())
        write_books(items)

# Three derivations of the cover, made once on upload. The original is often ~2 MB, which is
# wasteful to send a phone repeatedly; and iOS crops a tall cover awkwardly on the lock screen
# unless it's given something square, so that one is padded rather than cropped.
COVER_SIZES = {
    "thumb": "scale=200:-2",
    "full":  "scale=600:-2",
    "lock":  "scale=512:512:force_original_aspect_ratio=decrease,"
             "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x15131f",
}

def cover_path(book_id, size):
    return book_dir(book_id, f"cover-{size}.jpg")

def make_covers(book_id, raw):
    """raw = the original image bytes. Returns True if at least the thumbnail came out."""
    os.makedirs(book_dir(book_id), exist_ok=True)
    src = book_dir(book_id, "cover-src")
    with open(src, "wb") as f:
        f.write(raw)
    made = 0
    try:
        for size, vf in COVER_SIZES.items():
            r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", src, "-vf", vf,
                                "-q:v", "4", cover_path(book_id, size)],
                               capture_output=True, text=True, timeout=120)
            made += int(r.returncode == 0 and os.path.exists(cover_path(book_id, size)))
    finally:
        if os.path.exists(src): os.remove(src)
    return made > 0

def ensure_cover(book_id):
    """Covers are made on upload, but books added before that feature exists — or whose
    extraction failed — get one lazily from the stored EPUB rather than needing a re-add."""
    if os.path.exists(cover_path(book_id, "thumb")):
        return True
    src = book_dir(book_id, "book.epub")
    if not os.path.exists(src):
        return False
    try:
        raw = epub.cover(src)
    except Exception:
        return False
    return bool(raw) and make_covers(book_id, raw)

def split_segments(text, limit=SEGMENT_CHARS):
    """Cut a chapter into segment-sized pieces on sentence boundaries."""
    out, buf = [], ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 > limit and buf:
            out.append(buf.strip())
            buf = ""
        if len(para) > limit:                 # a single huge paragraph: cut it at sentences
            pending = para
            while len(pending) > limit:
                pieces, pending = cut_sentences(pending[:limit], 0, flush=True)[0], pending[limit:]
                out.append(" ".join(pieces))
            buf = (buf + " " + pending).strip()
        else:
            buf = (buf + "\n" + para).strip()
    if buf.strip():
        out.append(buf.strip())
    return out

def split_chunks(text, limit=CHUNK_CHARS):
    """Segment -> TTS-sized chunks, so run_lock is released between calls and a chat reply or
    a transcription can get in. Whole sentences only."""
    out, buf = [], ""
    for para in text.split("\n"):
        sentences, tail = cut_sentences(para.strip() + " ", 0, flush=True)
        for s in sentences:
            if len(buf) + len(s) + 1 > limit and buf:
                out.append(buf.strip())
                buf = ""
            buf = (buf + " " + s).strip()
    if buf.strip():
        out.append(buf.strip())
    return out

def render_chapter(book_id, index):
    """Render one chapter to opus, a segment at a time. Marks progress in books.json as it
    goes so the page can show it."""
    with render_lock:
        book = find_book(book_id)
        if not book:
            return
        chapters = book.get("chapters") or []
        if not (0 <= index < len(chapters)):
            return
        chapter = chapters[index]
        if chapter.get("state") == "ready":
            return
        voice = book.get("voice") or "af_heart"
        # Bumped whenever the narrator changes. A chapter that was already being rendered when
        # you switched would otherwise finish in the old voice and be marked ready, leaving one
        # chapter of the book in the wrong voice with nothing to show for it.
        gen = book.get("gen", 0)
        txt_path = book_dir(book_id, "text", f"ch{index:03d}.txt")
        try:
            with open(txt_path) as f:
                text = f.read()
        except OSError as e:
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="error", error=f"missing text: {e}"[:200]))
            return
        # The chapter's own heading line is a bare number ("9") or the title, which the spoken
        # lead-in says better, so it always comes out of the text.
        text = epub.strip_heading(text, chapter.get("name") or "")
        intro = chapter_intro(book, index)

        update_book(book_id, lambda b: b["chapters"][index].update(
            state="rendering", error=None, done=0, segments=[]))
        audio_dir = book_dir(book_id, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        segments = split_segments(text)
        made = []
        try:
            for si, seg_text in enumerate(segments):
                name = f"ch{index:03d}-s{si:02d}.opus"
                out  = os.path.join(audio_dir, name)
                if not os.path.exists(out):
                    # the closing pause belongs to the chapter, so only the last part gets it
                    _render_segment(seg_text, voice, out,
                                    intro=intro if si == 0 else None,
                                    tail_pause=CHAPTER_END_PAUSE if si == len(segments) - 1 else 0)
                made.append({"file": name, "seconds": audio_seconds(out)})
                # publish each finished segment: you can start listening to segment 1 while
                # segment 2 is still being made
                update_book(book_id, lambda b, m=list(made), n=len(segments):
                            b["chapters"][index].update(segments=m, done=len(m), total=n))
            if (find_book(book_id) or {}).get("gen", 0) != gen:
                # the narrator changed while this was rendering — throw it away rather than
                # leaving one chapter spoken by the previous voice
                for m in made:
                    p = os.path.join(audio_dir, m["file"])
                    if os.path.exists(p): os.remove(p)
                update_book(book_id, lambda b: b["chapters"][index].update(
                    state="pending", segments=[], error=None))
                return
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="ready", error=None,
                seconds=round(sum(s["seconds"] for s in made), 1)))
        except Exception as e:
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="error", error=str(e)[:200]))

PART_SEP = " · "     # how epub.py joins a part name to its chapter label

# Spoken lead-in before a chapter's text: "The Night Knocker" … "one" … the prose. The pause
# after each is real silence, not punctuation — a full stop buys about a third of a second,
# which isn't enough to read as "a new chapter is starting".
PART_PAUSE    = 1.2
CHAPTER_PAUSE = 0.9
# And a longer one at the end, so a chapter closes rather than running straight into the next
# announcement — the moment you'd use to notice a chapter has ended.
CHAPTER_END_PAUSE = 1.8

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def number_word(n):
    """Chapter numbers as words. Kokoro reads a bare "21" acceptably, but "twenty-one" is
    unambiguous and doesn't risk being read as a year or a list item."""
    if n < 0 or n > 999:
        return str(n)
    if n < 20:
        return _ONES[n] or "zero"
    if n < 100:
        return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")
    rest = n % 100
    return _ONES[n // 100] + " hundred" + (f" {number_word(rest)}" if rest else "")

def chapter_intro(book, index):
    """[(phrase, pause_after)] to speak before the chapter — the part's name when this chapter
    opens a new part, then the chapter number. Empty when announcements are off, and for a
    section that is neither numbered nor inside a part (an epigraph, say)."""
    if not book.get("announce", True):
        return []
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        return []
    name  = chapters[index].get("name") or ""
    part  = part_of(name)
    label = name.split(PART_SEP, 1)[1] if PART_SEP in name else name
    pieces = []
    if part and not any(part_of(c.get("name")) == part for c in chapters[:index]):
        pieces.append((part, PART_PAUSE))          # only when the part actually starts
    m = re.search(r"\d+", label)
    if m:
        pieces.append((number_word(int(m.group(0))), CHAPTER_PAUSE))
    return pieces

def part_of(name):
    return (name or "").split(PART_SEP)[0] if PART_SEP in (name or "") else ""

def chapters_in(book, part=None):
    """Chapters belonging to one part of the book, or all of them when part is None."""
    chapters = book.get("chapters") or []
    if not part:
        return chapters
    return [c for c in chapters if part_of(c.get("name")) == part]

def book_parts(book):
    """The book's top-level divisions, in order, with how much of each is narrated. Stand-alone
    sections that aren't inside a part (an epigraph, say) are reported under ''."""
    out, seen = [], {}
    for c in book.get("chapters") or []:
        p = part_of(c.get("name"))
        if p not in seen:
            seen[p] = {"part": p, "chapters": 0, "ready": 0, "words": 0, "first": c["i"]}
            out.append(seen[p])
        seen[p]["chapters"] += 1
        seen[p]["ready"] += int(c.get("state") == "ready")
        seen[p]["words"] += c.get("words", 0)
    return out

def render_all_worker(book_id, part=None):
    """Narrate every chapter, in order, until done or told to stop.

    Deliberately calls render_chapter per chapter rather than holding render_lock for the
    whole book: an 8-hour job that blocked every other render would be intolerable, and this
    way tapping a single chapter gets its turn between two chapters of the bulk run."""
    while True:
        book = find_book(book_id)
        if not book or not (book.get("render_all") or {}).get("running"):
            break
        # only "pending" — a chapter that errored is skipped rather than retried forever
        nxt = next((c["i"] for c in chapters_in(book, part) if c.get("state") == "pending"), None)
        if nxt is None:
            break
        render_chapter(book_id, nxt)
        book = find_book(book_id) or {}
        done = sum(1 for c in book.get("chapters") or [] if c.get("state") == "ready")
        update_book(book_id, lambda b, n=done: b.setdefault("render_all", {}).update(
            done=n, total=len(b.get("chapters") or [])))
    update_book(book_id, lambda b: b.setdefault("render_all", {}).update(running=False))

def export_worker(jid, book_id, part=None):
    """Build one .m4b: every narrated chapter, chapter markers, cover art, metadata.

    An audiobook file plays offline in software designed for it — chapters, sleep timer,
    position — which is more than this app's <audio> element will ever do."""
    job = jobs[jid]
    job["status"] = "collecting"
    tmpdir = tempfile.mkdtemp(prefix="m4b-")
    try:
        book = find_book(book_id)
        if not book:
            raise RuntimeError("unknown book")
        audio_dir = book_dir(book_id, "audio")
        wanted = chapters_in(book, part)
        if not wanted:
            raise RuntimeError(f"no chapters in “{part}”")
        parts, marks, clock, skipped = [], [], 0.0, 0
        for c in wanted:
            segs = c.get("segments") or []
            if c.get("state") != "ready" or not segs:
                skipped += 1
                continue
            start = clock
            for s in segs:
                p = os.path.join(audio_dir, s["file"])
                if not os.path.exists(p):
                    continue
                parts.append(p)
                clock += s.get("seconds") or audio_seconds(p)
            if clock > start:
                # inside a part the "Part · Chapter 3" prefix is just noise on every marker
                label = c.get("name") or f"Chapter {c['i']+1}"
                marks.append((start, clock,
                              label.split(PART_SEP, 1)[1] if part and PART_SEP in label else label))
        if not parts:
            raise RuntimeError("nothing narrated yet — render some chapters first")

        listing = os.path.join(tmpdir, "list.txt")
        with open(listing, "w") as f:
            for p in parts:
                f.write("file '%s'\n" % p.replace("'", r"'\''"))
        # ffmetadata carries the chapter marks; TIMEBASE 1/1000 means milliseconds
        title = f"{book['title']} — {part}" if part else book["title"]
        meta = [";FFMETADATA1", f"title={title}", f"album={book['title']}",
                f"artist={book.get('author') or ''}", "genre=Audiobook"]
        for start, end, name in marks:
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start*1000)}",
                     f"END={int(end*1000)}", f"title={name}"]
        metafile = os.path.join(tmpdir, "meta.txt")
        with open(metafile, "w") as f:
            f.write("\n".join(meta) + "\n")

        os.makedirs(book_dir(book_id, "export"), exist_ok=True)
        safe = re.sub(r"[^\w\- ]+", "", title).strip()[:80] or "audiobook"
        name = f"{safe}.m4b"
        out  = book_dir(book_id, "export", name)
        cover = cover_path(book_id, "full") if ensure_cover(book_id) else None

        job.update(status="encoding", seconds=round(clock, 1))
        cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listing,
               "-i", metafile]
        if cover:
            cmd += ["-i", cover]
        cmd += ["-map", "0:a", "-map_metadata", "1"]
        if cover:
            # attached_pic is what makes players show it as the book's artwork
            cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        # 48 kbps AAC mono: the source is already 32 kbps opus, so this adds little loss
        # while staying in the format Apple Books and every audiobook player reads.
        cmd += ["-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-300:])
        # The name keeps its spaces — it's what Apple Books will show — so the URL has to be
        # encoded rather than handed over raw.
        job.update(status="done", url=f"/export/{book_id}/{urllib.parse.quote(name)}", file=name,
                   text=f"{len(marks)} chapters" + (f", {skipped} not narrated" if skipped else ""),
                   seconds=round(clock, 1))
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def pad_with_silence(src, seconds, dst):
    """Append silence to a rendered clip. apad rather than a separately generated silence
    file: it re-encodes this wav in place, so the padding can't disagree with the engine's
    sample rate and break the concat (Kokoro is 24 kHz, Piper voices are 22.05).
    Returns the padded file, or the original if ffmpeg couldn't do it."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", src,
                        "-af", f"apad=pad_dur={seconds}", dst],
                       capture_output=True, text=True, timeout=120)
    return dst if r.returncode == 0 and os.path.exists(dst) else src

def _render_segment(text, voice, out_path, intro=None, tail_pause=0):
    """One segment = many TTS calls concatenated. run_lock is taken per chunk, not for the
    whole segment, so hours of narration don't starve everything else.

    `intro` is [(phrase, pause_after)] spoken first — the part name and chapter number.
    `tail_pause` is silence appended at the very end, for the last segment of a chapter."""
    tmpdir = tempfile.mkdtemp(prefix="book-")
    parts = []
    try:
        for ii, (phrase, pause) in enumerate(intro or []):
            raw = os.path.join(tmpdir, f"intro-{ii}.wav")
            with run_lock:
                tts_say(voice, respell(phrase), 1.0, raw)
            parts.append(pad_with_silence(raw, pause, os.path.join(tmpdir, f"intro-{ii}-pad.wav")))
        for ci, chunk in enumerate(split_chunks(text)):
            wav = os.path.join(tmpdir, f"{ci:04d}.wav")
            with run_lock:
                tts_say(voice, respell(chunk), 1.0, wav)
            parts.append(wav)
        if not parts:
            raise RuntimeError("nothing to say in this segment")
        if tail_pause:
            # pad the final clip rather than adding a silent one, for the same format reason
            parts[-1] = pad_with_silence(parts[-1], tail_pause,
                                         os.path.join(tmpdir, "tail-pad.wav"))
        # The audio directory can vanish underneath a render — changing the narrator deletes
        # it — and ffmpeg's failure then reads as a mysterious "No such file or directory".
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        listing = os.path.join(tmpdir, "list.txt")
        with open(listing, "w") as f:
            for p in parts:
                f.write(f"file '{p}'\n")
        # 32 kbps opus: ~290 MB for a 20 hour book, against 3.5 GB as wav
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                            "-i", listing, "-c:a", "libopus", "-b:a", "32k", out_path],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-200:])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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

# ---- books ----
@app.post("/api/books")
def api_book_add():
    """Take an EPUB, pull the chapters out, and keep the prose on disk ready to narrate."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no file"), 400
    bid = uuid.uuid4().hex[:12]
    os.makedirs(book_dir(bid, "text"), exist_ok=True)
    src = book_dir(bid, "book.epub")
    f.save(src)
    try:
        meta, chapters, skipped = epub.extract(src)
    except Exception as e:
        shutil.rmtree(book_dir(bid), ignore_errors=True)
        return jsonify(ok=False, msg=f"couldn't read that EPUB: {str(e)[:150]}"), 400
    if not chapters:
        shutil.rmtree(book_dir(bid), ignore_errors=True)
        return jsonify(ok=False, msg="no readable chapters in that EPUB"), 400
    for i, c in enumerate(chapters):
        with open(book_dir(bid, "text", f"ch{i:03d}.txt"), "w") as fh:
            fh.write(c["text"])
    try:
        raw_cover = epub.cover(src)
    except Exception:
        raw_cover = None
    has_cover = bool(raw_cover) and make_covers(bid, raw_cover)
    # A Dutch book should land on a Dutch voice without being told.
    dutch = (meta.get("language") or "").startswith("nl")
    voice = (piper_voice_ids()[0] if dutch and piper_voice_ids() else "af_heart")
    entry = {"id": bid, "cover": has_cover,
             "title": meta["title"], "author": meta["author"],
             "language": meta["language"], "voice": voice, "announce": True,
             "added": int(time.time()), "updated": int(time.time()),
             "position": {"chapter": 0, "segment": 0, "offset": 0},
             "skipped": skipped[:40],
             "chapters": [{"i": i, "name": c["name"], "words": c["words"],
                           "state": "pending", "segments": [], "error": None}
                          for i, c in enumerate(chapters)]}
    with index_lock:
        items = load_books()
        items.insert(0, entry)
        write_books(items)
    return jsonify(ok=True, book=book_summary(entry))

@app.get("/api/books")
def api_books():
    return jsonify(books=[book_summary(b) for b in load_books()])

@app.get("/api/books/<book_id>")
def api_book(book_id):
    b = find_book(book_id)
    if not b:
        return jsonify(error="unknown book"), 404
    return jsonify(book=b, parts=book_parts(b))

@app.post("/api/books/update")
def api_book_update():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    # Changing voice or heading handling makes the existing audio wrong — it was made with the
    # old setting, and mixing two narrators inside one book would be worse than re-rendering.
    # Only the chapter you're actually at is re-made now, though; the rest come back when you
    # reach them, so switching narrator costs one chapter's wait, not the whole book's.
    resets = ((d.get("voice") and d["voice"] != book.get("voice"))
              or (d.get("announce") is not None
                  and bool(d["announce"]) != bool(book.get("announce", True))))
    chapters = book.get("chapters") or []
    # Nothing rendered yet means nothing to throw away: just change it.
    if resets and not any(c.get("state") == "ready" for c in chapters):
        resets = False
        d.pop("confirm", None)
    resume = None
    if resets:
        ready = [c["i"] for c in chapters if c.get("state") == "ready"]
        # "where you'd carry on from": your listening position, or the furthest chapter that
        # had been narrated if you'd rendered ahead of yourself
        resume = max([(book.get("position") or {}).get("chapter", 0)] + ready) if chapters else 0
    if resets and not d.get("confirm"):
        rendered = sum(1 for c in book.get("chapters") or [] if c.get("state") == "ready")
        name = (book.get("chapters") or [{}])[resume].get("name", f"chapter {resume + 1}") \
               if book.get("chapters") else ""
        return jsonify(ok=False, needs_confirm=True, rendered=rendered, resume=resume,
                       msg=(f"the audio for {rendered} chapter(s) was made with the old voice "
                            f"and gets discarded — only “{name}” is re-made now"), ), 409
    def apply(b):
        if d.get("title"):  b["title"] = d["title"][:200]
        if d.get("voice") and tts_engine_of(d["voice"]): b["voice"] = d["voice"]
        if d.get("announce") is not None: b["announce"] = bool(d["announce"])
        if isinstance(d.get("position"), dict): b["position"] = d["position"]
        if resets:
            b["gen"] = b.get("gen", 0) + 1        # invalidates anything mid-render
            b.setdefault("render_all", {})["running"] = False
            for c in b["chapters"]:
                c.update(state="pending", segments=[], error=None)
    update_book(book["id"], apply)
    if resets:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
        # Re-make just the one you'd carry on from, so the new narrator is ready to listen to
        # without re-rendering everything you'd already been through.
        threading.Thread(target=render_chapter, args=(book["id"], resume), daemon=True).start()
    return jsonify(ok=True, book=find_book(book["id"]), resume=resume)

@app.post("/api/books/delete")
def api_book_delete():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    with index_lock:
        write_books([b for b in load_books() if b.get("id") != book["id"]])
    shutil.rmtree(book_dir(book["id"]), ignore_errors=True)
    return jsonify(ok=True)

@app.post("/api/books/render")
def api_book_render():
    """Ask for a chapter (and optionally the one after it, to stay ahead of the listener)."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    wanted = [index] + ([index + 1] if d.get("ahead") else [])
    started = []
    for i in wanted:
        chapters = book.get("chapters") or []
        if 0 <= i < len(chapters) and chapters[i].get("state") in ("pending", "error"):
            threading.Thread(target=render_chapter, args=(book["id"], i), daemon=True).start()
            started.append(i)
    return jsonify(ok=True, started=started)

@app.post("/api/books/clear")
def api_book_clear():
    """Throw away the narration, keep the book. Until now the only ways to clear a book's
    audio were changing the narrator or deleting the whole thing."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    def apply(b):
        b["gen"] = b.get("gen", 0) + 1          # stops anything mid-render being kept
        b.setdefault("render_all", {})["running"] = False
        for c in b.get("chapters") or []:
            c.update(state="pending", segments=[], error=None, seconds=None)
        b["position"] = {"chapter": 0, "segment": 0, "offset": 0}
    update_book(book["id"], apply)
    shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
    shutil.rmtree(book_dir(book["id"], "export"), ignore_errors=True)
    return jsonify(ok=True, book=find_book(book["id"]))

@app.post("/api/books/rescan")
def api_book_rescan():
    """Re-read the stored EPUB — for when extraction has improved since the book was added.

    Keeps the narrated audio, but only when the chapters still line up exactly: same count,
    same word counts, in the same order. If anything moved, the existing audio might belong
    to different text, so it refuses rather than quietly mismatching sound and chapter.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    src = book_dir(book["id"], "book.epub")
    if not os.path.exists(src):
        return jsonify(ok=False, msg="the original EPUB isn't stored for this book"), 400
    try:
        meta, chapters, skipped = epub.extract(src)
    except Exception as e:
        return jsonify(ok=False, msg=f"couldn't re-read it: {str(e)[:150]}"), 400
    old = book.get("chapters") or []
    same = (len(chapters) == len(old)
            and all(c["words"] == o.get("words") for c, o in zip(chapters, old)))
    if not same and not d.get("confirm"):
        return jsonify(ok=False, needs_confirm=True,
                       msg=(f"the chapters changed ({len(old)} → {len(chapters)}), so the "
                            "narrated audio no longer matches and would be discarded")), 409
    for i, c in enumerate(chapters):
        with open(book_dir(book["id"], "text", f"ch{i:03d}.txt"), "w") as fh:
            fh.write(c["text"])
    def apply(b):
        b["title"], b["author"] = meta["title"], meta["author"]
        b["skipped"] = skipped[:40]
        keep = {o["i"]: o for o in (b.get("chapters") or [])} if same else {}
        b["chapters"] = [{"i": i, "name": c["name"], "words": c["words"],
                          "state": keep.get(i, {}).get("state", "pending"),
                          "segments": keep.get(i, {}).get("segments", []),
                          "seconds": keep.get(i, {}).get("seconds"),
                          "error": keep.get(i, {}).get("error")}
                         for i, c in enumerate(chapters)]
        if not same:
            b["position"] = {"chapter": 0, "segment": 0, "offset": 0}
    update_book(book["id"], apply)
    if not same:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
    return jsonify(ok=True, kept_audio=same, book=find_book(book["id"]))

@app.post("/api/books/render_all")
def api_book_render_all():
    """Narrate the whole book — hours of work, so it reports progress and can be stopped."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    if (book.get("render_all") or {}).get("running"):
        return jsonify(ok=True, already=True)
    part = d.get("part") or None
    scope = chapters_in(book, part)
    done = sum(1 for c in scope if c.get("state") == "ready")
    update_book(book["id"], lambda b: b.update(render_all={
        "running": True, "done": done, "total": len(scope), "part": part}))
    threading.Thread(target=render_all_worker, args=(book["id"], part), daemon=True).start()
    return jsonify(ok=True)

@app.post("/api/books/render_stop")
def api_book_render_stop():
    d = request.get_json(force=True, silent=True) or {}
    if not find_book(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown book"), 404
    # the worker checks this between chapters; the one in flight finishes rather than
    # leaving a half-made chapter behind
    update_book(d["id"], lambda b: b.setdefault("render_all", {}).update(running=False))
    return jsonify(ok=True)

@app.post("/api/books/export")
def api_book_export():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(error="unknown book"), 404
    jid = new_job("export")
    threading.Thread(target=export_worker, args=(jid, book["id"], d.get("part") or None),
                     daemon=True).start()
    return jsonify(job_id=jid)

@app.get("/export/<book_id>/<path:filename>")
def book_export(book_id, filename):
    path = safe_path(book_dir(book_id, "export"), filename)
    if not path:
        return jsonify(error="not found"), 404
    return send_from_directory(book_dir(book_id, "export"), filename,
                               as_attachment=True, conditional=True)

@app.post("/api/books/cover")
def api_book_cover():
    """Replace the cover by hand — for books that declare none, or when you'd rather use a
    different image than the publisher's."""
    bid = request.form.get("id") or ""
    book = find_book(bid)
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no image"), 400
    if not make_covers(bid, f.read()):
        return jsonify(ok=False, msg="couldn't read that image"), 400
    update_book(bid, lambda b: b.update(cover=True))
    return jsonify(ok=True)

@app.get("/cover/<book_id>/<size>.jpg")
def book_cover(book_id, size):
    if size not in COVER_SIZES:
        return jsonify(error="unknown size"), 404
    if not ensure_cover(book_id) or not os.path.exists(cover_path(book_id, size)):
        return jsonify(error="no cover"), 404
    r = send_from_directory(book_dir(book_id), f"cover-{size}.jpg", conditional=True)
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r

@app.get("/book/<book_id>/<path:filename>")
def book_audio(book_id, filename):
    path = safe_path(book_dir(book_id, "audio"), filename)
    if not path:
        return jsonify(error="not found"), 404
    # Cacheable on purpose: during the lock-screen test no-store made Safari re-fetch the
    # same file several times. send_from_directory handles range requests, so seeking works.
    r = send_from_directory(book_dir(book_id, "audio"), filename, conditional=True)
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r

@app.get("/api/models")
def api_models():
    """Installed Ollama models. Reports the reason when Ollama is down, so the chat panel
    can say 'run ollama serve' instead of just failing."""
    try:
        return jsonify(ok=True, models=ollama_models(), default=DEFAULT_CHAT_MODEL)
    except Exception as e:
        return jsonify(ok=False, models=[], error=ollama_error(e))

@app.get("/api/chats")
def api_chats():
    return jsonify(chats=[chat_summary(c) for c in load_chats()], system=DEFAULT_SYSTEM)

@app.get("/api/chats/<chat_id>")
def api_chat_get(chat_id):
    chat = find_chat(chat_id)
    if not chat:
        return jsonify(error="unknown chat"), 404
    return jsonify(chat=chat)

@app.post("/api/chats")
def api_chat_new():
    d = request.get_json(force=True, silent=True) or {}
    entry = {"id": uuid.uuid4().hex[:12],
             "name": (d.get("name") or "").strip() or "New chat",
             "model": (d.get("model") or DEFAULT_CHAT_MODEL).strip(),
             "system": d.get("system") if d.get("system") is not None else DEFAULT_SYSTEM,
             # what you speak, not what it answers in — see LANG_NOTE
             "language": d.get("language") if d.get("language") in LANGS else "en",
             "created": int(time.time()), "updated": int(time.time()), "messages": []}
    with index_lock:
        items = load_chats()
        items.insert(0, entry)          # newest first, like clips and presets
        write_chats(items)
    return jsonify(ok=True, chat=entry)

@app.post("/api/chats/update")
def api_chat_update():
    d = request.get_json(force=True, silent=True) or {}
    if not find_chat(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown chat"), 404
    with index_lock:
        items = load_chats()
        for c in items:
            if c.get("id") != d["id"]: continue
            for key in ("name", "model", "system"):
                if d.get(key) is not None: c[key] = d[key]
            if d.get("language") in LANGS: c["language"] = d["language"]
            c["updated"] = int(time.time())
        write_chats(items)
    return jsonify(ok=True, chat=find_chat(d["id"]))

@app.post("/api/chats/delete")
def api_chat_delete():
    d = request.get_json(force=True, silent=True) or {}
    if not find_chat(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown chat"), 404
    with index_lock:
        write_chats([c for c in load_chats() if c.get("id") != d["id"]])
    return jsonify(ok=True)

@app.post("/api/chats/clear")
def api_chat_clear():
    """Wipe the transcript but keep the chat, its model and its system prompt."""
    d = request.get_json(force=True, silent=True) or {}
    if not find_chat(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown chat"), 404
    with index_lock:
        items = load_chats()
        for c in items:
            if c.get("id") == d["id"]:
                c["messages"] = []
                c["updated"]  = int(time.time())
        write_chats(items)
    return jsonify(ok=True)

@app.post("/api/chat")
def api_chat():
    d    = request.get_json(force=True, silent=True) or {}
    text = (d.get("text") or "").strip()
    chat = find_chat(d.get("chat_id") or "")
    if not chat:
        return jsonify(error="unknown chat"), 404
    if not text:
        return jsonify(error="type something first"), 400
    model = (d.get("model") or chat.get("model") or DEFAULT_CHAT_MODEL).strip()
    # The voice is sent whether or not it will be spoken, because it decides what language the
    # reply should be in; `speak` is what turns on sentence-by-sentence rendering.
    voice  = d.get("voice") if tts_engine_of(d.get("voice") or "") else None
    engine = tts_engine_of(voice) if voice else None
    try:
        speed = min(2.0, max(0.5, float(d.get("speed") or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    language = d.get("language") if d.get("language") in LANGS else (chat.get("language") or "en")
    # Only force English when the voice can't speak Dutch. With a Piper voice selected the
    # model is left alone and answers in Dutch, which is the whole point of having it.
    english_only = (language == "nl" and engine != "piper")
    jid = new_job("chat")
    threading.Thread(target=chat_worker,
                     args=(jid, chat["id"], text, model, voice if d.get("speak") else None,
                           speed, english_only),
                     daemon=True).start()
    return jsonify(job_id=jid)

@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    return jsonify(job)

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

def clear_stale_state():
    """The workers live in this process, so a restart kills them. Anything still marked as
    running is a leftover, and would otherwise show a progress bar that never moves."""
    items = load_books()
    changed = False
    for b in items:
        if (b.get("render_all") or {}).get("running"):
            b["render_all"]["running"] = False
            changed = True
        for c in b.get("chapters") or []:
            if c.get("state") == "rendering":
                c.update(state="pending", segments=[], error=None)
                changed = True
    if changed:
        write_books(items)

if __name__ == "__main__":
    clear_stale_state()
    if TTS_IDLE_SECONDS > 0:
        threading.Thread(target=worker_reaper, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
