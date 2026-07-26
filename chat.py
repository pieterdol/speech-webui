"""Chat against a local Ollama, optionally spoken as it arrives."""
import json, os, queue, re, threading, time, urllib.error, urllib.parse, urllib.request, uuid

from flask import jsonify, request

from core import HERE, LANGS, app, index_lock, jobs, new_job, run_lock, write_json
from textprep import cut_sentences, respell, speech_text
from tts import OUT_DIR, tts_engine_of, tts_say

CHATS_FILE = os.path.join(HERE, "chats.json")

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# and a Kokoro render use different hardware and should overlap. It's still a lock —
# two chats at once would just thrash one GPU (OLLAMA_NUM_PARALLEL is 1 anyway).
chat_lock = threading.Lock()


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
