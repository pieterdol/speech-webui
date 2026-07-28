# Architecture

How the pieces fit, what owns what, and the settings worth knowing about.

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro_worker   (subprocess, English, resident)
                                              ├── piper_worker    (subprocess, Dutch, resident)
                                              ├── f5-tts CLI      (~/.local/bin/f5-tts)
                                              ├── ollama HTTP     (127.0.0.1:11434 → Radeon)
                                              └── openlibrary.org (once per book added)
```

Every arrow but the last one stays on this machine. `openlib.py` sends a title and an author when
a book is added and gets back a line about what it is; nothing else, and nothing waits on it.

## The speech engines

**Both TTS engines have their own venvs**, so the app borrows their interpreters rather than
duplicating their dependencies — F5-TTS via its CLI per render, Kokoro via the resident worker below.
Whisper is the exception: it's the most-used path, so `faster-whisper` lives in this app's venv with
the model resident between requests.

**Kokoro is resident, and deliberately not in this app's venv.** The `kokoro-tts` CLI reloads its 325
MB ONNX model on every invocation, ~1.9 s of fixed cost per render against ~0.3 s for a resident
worker — more than the audio itself for a short sentence. `kokoro_worker.py` keeps the model loaded
and takes one JSON request per line over a pipe, launched with **Kokoro's own interpreter**
(`~/.local/share/kokoro-tts/venv/bin/python`) so its ONNX dependencies stay where they already are.

The worker costs 530 MB–1 GB of RAM while loaded, so it **unloads itself after 10 minutes with no
renders** and starts again on the next one, a restart of about 1.4 s. `KOKORO_IDLE_MINUTES` changes
the window; `0` keeps it resident. Killing it by hand is safe at any time — the next render restarts
it.

**Kokoro reads 510 phonemes at a time**, and the worker does the splitting rather than leaving it to
`kokoro-onnx`, whose own splitter only breaks at punctuation: text without any — a page of book
titles, one per line — comes back as one oversized batch, and the model then indexes the voice's
style table by the token count and falls off the end of it. So the text is phonemized once, cut at the
latest punctuation, space or character that fits under the limit, and the pieces are read in turn and
joined. It has to be counted in phonemes, not characters: English runs about 1.1 phonemes per
character and *"$100,000"* is thirty, so no limit on the text could stand in for it.

**A voice's language comes from its prefix** — `bm_george` → `en-gb` — otherwise British voices get
phonemized with US rules and sound wrong. The pickers list the American and British voices only; the
other forty-odd are still on `/api/voices`, filtered out of the dropdowns by `GROUPS` in
`fillVoices()`. Kokoro and Piper share one voice picker and the engine dropdown decides which set it
shows; neither needs naming when you ask for speech, since a voice belongs to exactly one engine and
`/api/speak` works it out from the voice name.

A Piper multi-speaker model, if one is installed, is offered as one voice per speaker
(`nl_NL-mls-medium-2450`), all sharing the one loaded model.

## Locks and jobs

**Two locks, not one.** `run_lock` serializes the CPU model work (Whisper, Kokoro, F5-TTS) because
F5-TTS saturates all 12 cores. Chat gets its own `chat_lock`: Ollama offloads the whole model to the
GPU, so a reply and a Kokoro render use different hardware and are free to overlap — putting chat
behind `run_lock` would make it wait out a two-minute clone for no reason.

**Rendering shares the machine, and survives interruption.** `run_lock` is taken per ~600-character
chunk and released between them, so a chat reply or a transcription slots in between rather than
waiting out a chapter; one book renders at a time. Between segments a render checks whether its book
still exists and whether the narrator has changed, so a delete or a voice switch takes effect within
one segment and what that render made is thrown away. A render killed by a restart instead keeps its
finished parts: they're real audio, they play, they can be exported, and re-rendering the chapter
reuses them.

**Long jobs are threads** writing into a `jobs` dict that the page polls at `/api/status/<id>`, the
same pattern as `~/Code/comfy-webui`. The dict is in memory, so restarting drops running jobs and the
page says so if you poll one the server no longer knows. Chat *messages* survive, in `chats.json`;
only a reply still being written is lost.

**Narration is files rather than streaming** because iOS suspends a page's timers when the screen
locks, so the sentence-at-a-time approach Chat uses would stall the moment the phone goes in a
pocket. Files don't: with the phone locked, parts play back to back, each advancing in under a
second, which is what makes ~10-minute parts safe.

## Chat

**Ollama cold loads.** On the Vulkan backend loading `qwen3:8b` takes about 4 seconds; after that it
stays resident for `keep_alive` and follow-ups start instantly, and the status line says which of the
two is happening. The per-request `keep_alive` in `speech.py` overrides the server's
`OLLAMA_KEEP_ALIVE`, so change it there; it's short because a resident 8B model holds ~5.2 GB of the
card's 16 GB.

**Thinking is disabled.** `qwen3` is a hybrid reasoning model; left alone it emits a `<think>` block
that Kokoro would happily read aloud. The app sends `think: false` — but only to models whose Ollama
capabilities include `thinking`, because `qwen2.5-coder` rejects the flag. Any `<think>` block that
slips through is stripped before the reply is stored.

**Getting an English answer to a Dutch question takes three things together**, and applies only when
an English voice is selected. `qwen3:8b` mirrors the language of the latest user turn and a
system-prompt instruction alone doesn't hold against that, so the app also sends a `[Reply in
English.]` marker on the turn itself and a two-exchange primer answering a Dutch question in English.
Both are attached to what gets **sent**, never to what's stored, so `chats.json` and the on-screen
transcript stay clean.

## Modules

One file per feature, plus a thin `core.py` holding what they share: the Flask app, the job table,
the locks that keep one model on the GPU at a time, and two file helpers. It exists so `books.py` and
`chat.py` can both reach the app without importing each other.

`write_json`, one of those two helpers, writes every index (`clips.json`, `presets.json`,
`chats.json`, `books.json`) to a temp file and renames it, so a read that lands mid-write gets a
whole file rather than half of one — a whole-book render rewrites the book index after every chapter
while the page is polling it. `safe_path`, the other, is the app's only security boundary.

**Routes are registered by importing the modules for their side effect** — `speech.py` imports all
five and each decorates the shared `app`. That's the one fragile part: a module that stops being
imported takes its endpoints with it and nothing else notices, so `tests/test_routes.py` writes the
whole URL table out and fails on any difference.

**Each module owns the storage it's responsible for** — `books.py` holds `BOOKS_DIR` and `BOOKS_FILE`
rather than taking them from core. That's what lets a test point the whole book layer at a tmpdir by
patching two names on one module.

The import order is `core` → `textprep`/`media` → `clips` → `tts` → `stt`/`chat`/`books`, and nothing
imports upwards. Functions are grouped by feature rather than wrapped in classes: this is functions
over a JSON document and a few locks, and there's no object model in it straining to get out.

`speech.py` is the entry point and is deliberately not named `app.py`, so `restart.sh` can't collide
with comfy-webui's.

**A chapter's number and its filename are two different things.** `i` is the position in
`book["chapters"]` — what everything means by a chapter, including the saved position — and files are
named after the number the chapter was *created* with, kept in `key` when the two differ. They only
differ in a book that has had a section put back into it; anywhere else `key` is absent and the two
are the same number, which is why nothing else in the app had to change. Every path goes through
`text_file`, `audio_file` and `audio_name`, so there's one place to check that.

## Settings

| Setting | Where | What it does |
| --- | --- | --- |
| `OLLAMA_URL` | environment | Where Ollama is, if not `http://127.0.0.1:11434` |
| `HIP_VISIBLE_DEVICES=-1`, `ROCR_VISIBLE_DEVICES=-1` | Ollama's environment | Not optional on this PC — see [gotchas.md](gotchas.md) |
| `KOKORO_IDLE_MINUTES` | environment | Idle window before the Kokoro worker unloads; `0` keeps it resident |
| `OPENLIBRARY` | environment | `0` stops a book looking itself up when it's added, and then nothing leaves this machine on its own. **↻** in ⚙ still asks, since tapping it is the request |
| `keep_alive` | `speech.py`, per request | How long Ollama keeps a model in VRAM; overrides `OLLAMA_KEEP_ALIVE` |
| `num_ctx` | `speech.py` | Chat context, 8192 tokens |
| `STRICT_TESTS` | environment | Fail instead of skipping when a tool is missing — see [testing.md](testing.md) |
| port 8600 | `speech.py` | The app itself |
| port 8443 | `serve.sh` | The tailnet HTTPS front, because 443 on this tailnet is already taken |
