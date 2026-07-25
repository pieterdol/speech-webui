# Local Speech Studio

A phone-friendly web front-end for the local speech tools on this PC. Record (or upload) a
clip, get the text out of it, and turn text back into speech — either with Kokoro's built-in
voices or by cloning a reference clip with F5-TTS. There's also a **Chat** mode: talk to a
local Qwen model through Ollama and have its replies read back to you in a Kokoro voice.
Everything runs locally — speech on the CPU, the language model on the Radeon; nothing is
sent to a cloud service.

The header has two modes: **Studio** (the three speech panels) and **Chat**.

## URLs

| Where | URL |
| --- | --- |
| Phone / tailnet | `https://your-machine.your-tailnet.ts.net:8443` |
| This PC | `http://127.0.0.1:8600` |

**The HTTPS URL matters.** Safari blocks the microphone (`getUserMedia`) and the clipboard
outside a secure context, so over plain http the record button and the Copy button don't work —
only the file picker does. `localhost` counts as secure, which is why the PC URL is fine.

## Running it

```bash
./run.sh        # foreground
./restart.sh    # kill + relaunch in the background (after editing speech.py)
./serve.sh      # one-time: publish on the tailnet over HTTPS via tailscale serve
tail -f speech.log
```

Port 8443 is used because `443` on this tailnet is already taken by another service.

## What it does

**1 · Record or add a clip.** One button records from the mic (Chrome does webm/opus, iOS does
mp4/aac — both are accepted). Everything is normalized through ffmpeg into 24 kHz mono wav in
`clips/`, which is what both Whisper and F5-TTS want internally. The file picker below it covers
the case where the recording was made elsewhere (iOS Voice Memos, exported to Files).

**2 · Speech → text.** faster-whisper on CPU. `small` is the default (a minute of audio in
~10-15 s); `large-v3-turbo` is the accurate, slower option. English and Dutch. The transcript
appears in a copyable block with a **Copy** button, and can be pushed straight into either of
the text fields in panel 3. Transcripts are cached per clip in `clips.json`.

**3 · Text → speech.**

- *Kokoro* — 54 built-in voices, a few seconds per sentence. The language code is derived from
  the voice prefix (`bm_george` → `en-gb`), otherwise British and non-English voices get
  phonemized with US rules and sound wrong.
- *F5-TTS* — clones a voice. Needs the exact transcript of the reference clip, which is
  auto-filled from Whisper when that clip has already been transcribed. Only clone your own
  voice, or one you have permission to use.

**Saved voices.** Once a reference clip sounds right, "Save this voice for reuse" stores it as a
named preset and the app defaults to it from then on — no re-picking the clip or retyping the
transcript. A preset keeps **its own copy** of the audio (trim already applied) in `presets/`, so
it keeps working after the clip it came from is deleted: clips are scratch space, presets last.

**Quality dial.** F5-TTS sampling steps are selectable: standard (32) or faster (16). Measured on
the same sentence, 16 steps took 66 s against ~120 s for 32, and Whisper transcribed the faster
output back word-perfect. Generation time scales with output length, so treat "about a minute a
sentence" as a guide rather than a promise.

Note that a saved voice does **not** make generation faster. F5-TTS is a zero-shot cloner: it
re-derives the voice from the reference on every run, and the minutes go into generating the
output waveform, not learning the voice. Presets save setup effort; the quality dial saves time.

## 4 · Chat (Qwen via Ollama, spoken by Kokoro)

**Ollama must be running** — the app talks to `http://127.0.0.1:11434` and does not start it:

```bash
HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve   # or OLLAMA_URL if it's elsewhere
```

**Those two variables are not optional on this PC** — see *Ollama picks the wrong GPU backend*
below. `ollama.service` in this repo is a systemd user unit that sets them for you.

If it isn't up, the chat panel says so instead of failing silently. Everything else in the app
keeps working without it.

- **Model** — every model installed in Ollama, Qwen first (`qwen3:8b` is the default). Sizes are
  shown because they have to fit in the 16 GB alongside anything ComfyUI has loaded: `qwen3:8b`
  is ~5.2 GB, `qwen3:14b` ~9.3 GB.
- **Voice** — the same 54 Kokoro voices as panel 3, remembered separately from the panel-3 pick.
- **🔊 Speak replies out loud** — off by default. On, each finished reply is sent to Kokoro and
  played; there's also a 🔊 button on every reply. A player is left on the message either way,
  because a browser can still refuse to autoplay.
- **🎤** — tap to talk, tap ⏹ to stop. Goes through the same Whisper path as panel 2 (using the
  model and language chosen there) and lands in the composer. With *Send as soon as I stop
  talking* on, it sends itself, so voice in → voice out needs two taps. A spoken turn is
  dictation, not a clip: it posts to `/api/dictate`, which transcribes and then deletes the
  audio, so nothing accumulates in `clips/` or the Studio list.
- **System prompt** — per chat. The default asks for short, plain-prose answers, because markdown
  bullets and code fences read terribly out loud.

Chats live in `chats.json` on the server rather than in the browser, so a conversation started
on the PC continues on the phone. The first thing you say names the chat.

**Thinking is disabled.** `qwen3` is a hybrid reasoning model; left alone it emits a `<think>`
block that Kokoro would happily read aloud. The app sends `think: false` — but only to models
whose Ollama capabilities include `thinking`, because `qwen2.5-coder` rejects the flag. Any
`<think>` block that slips through is stripped before the reply is stored.

**Cold loads.** On the Vulkan backend loading `qwen3:8b` takes about 4 seconds; after that it
stays resident for `keep_alive` (5 min) and follow-ups start instantly. The status line says
which of the two is happening. Note that the per-request `keep_alive` in `speech.py` overrides
the server's `OLLAMA_KEEP_ALIVE` — change it there, not in the unit file, to affect this app.
It's kept short on purpose: a resident 8B model holds ~5.2 GB of the card's 16 GB.

## How it fits together

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro_worker   (subprocess, model resident)
                                              ├── f5-tts CLI      (~/.local/bin/f5-tts)
                                              └── ollama HTTP     (127.0.0.1:11434 → Radeon)
```

**Kokoro is resident, and deliberately not in this app's venv.** The `kokoro-tts` CLI reloads
its 325 MB ONNX model on every invocation, which measured at ~1.9 s of fixed cost per render —
more than the audio itself for a short sentence. `kokoro_worker.py` keeps the model loaded and
takes one JSON request per line over a pipe. It's launched with **Kokoro's own interpreter**
(`~/.local/share/kokoro-tts/venv/bin/python`), so its 524 MB of ONNX dependencies stay where
they already are rather than being duplicated here.

| same text | CLI per render | resident worker |
| --- | --- | --- |
| "Hello there." (0.9 s audio) | 2.0 s | **0.6 s** |
| one sentence (4.2 s audio) | 3.0 s | **1.8 s** |
| eight sentences (24.5 s audio) | 11.1 s | **10.3 s** |

Fixed cost per render drops from ~1.9 s to ~0.3 s; the marginal cost (~0.38 s per second of
audio) is unchanged, so long text barely improves and short text improves a lot.

The worker costs 530 MB–1 GB of RAM while it's loaded, growing with the longest text rendered
(onnxruntime widens its arena and keeps it), so it **unloads itself after 10 minutes with no
renders** and starts again on the next one. That restart costs about 1.4 s (2.2 s against 0.8 s
warm), which is well worth ~700 MB back on an idle machine. `KOKORO_IDLE_MINUTES` changes the
window; `0` disables the reaper and keeps it resident. Killing the worker by hand is also safe
at any time — the next render just restarts it.

Two locks, not one. `run_lock` serializes the CPU model work (Whisper, Kokoro, F5-TTS) because
F5-TTS saturates all 12 cores. Chat gets its own `chat_lock`: Ollama offloads the whole model to
the GPU, so a reply and a Kokoro render use different hardware and are free to overlap — putting
chat behind `run_lock` would make it wait out a two-minute clone for no reason.

Both TTS engines have their own venvs (`~/.local/share/kokoro-tts`, `~/.local/share/f5-tts`), so
the app borrows their interpreters rather than duplicating their dependencies — F5-TTS via its
CLI per render, Kokoro via the resident worker above. Whisper is the exception: it's the
most-used path, so `faster-whisper` lives in this app's venv and the model stays resident
between requests. All three model paths are now warm between calls.

Long jobs are threads writing into a `jobs` dict that the page polls at `/api/status/<id>`, the
same pattern as `~/Code/comfy-webui`. A single lock serializes all model work: F5-TTS saturates
all 12 cores, so overlapping a clone with a transcription would just make both crawl.

## Layout

```
speech.py     Flask app (port 8600). Named speech.py, not app.py, so restart.sh can't
              collide with comfy-webui's (which kills anything matching "app.py").
kokoro_worker.py  resident Kokoro process, run with Kokoro's venv interpreter
index.html    the whole UI — one file, no build step
clips/        normalized input clips (gitignored)
presets/      saved voices — own copy of the reference audio (gitignored)
outputs/      generated audio (gitignored)
clips.json    clip index + cached transcripts (gitignored)
presets.json  saved voices: name, reference transcript, source clip (gitignored)
chats.json    chat transcripts, per-chat model and system prompt (gitignored)
```

## Gotchas

- **No mic on plain http** — use the HTTPS URL. The UI says so if it detects an insecure context.
- **Whisper model downloads** happen on first use (`small` ≈ 500 MB, `large-v3-turbo` ≈ 1.6 GB)
  into `~/.cache/huggingface`; the first transcription after a switch is slower.
- **F5-TTS needs the reference transcript.** If it's blank the CLI would download and run its
  own Whisper, so the app rejects the request instead.
- **Restarting drops running jobs** — the `jobs` dict is in memory. The page says so if you poll
  a job the server no longer knows. Chat *messages* survive (they're in `chats.json`); only a
  reply still being written is lost.
- **Ollama picks the wrong GPU backend.** Ollama 0.32.0 (installed 2026-07-14) ships only a
  ROCm 7.2 runtime, and ROCm 7 dropped consumer RDNA2 — which is what the RX 6900 XT (gfx1030)
  is. Left alone Ollama chooses ROCm and the upload to VRAM crawls: measured here, a 732 MiB
  model didn't load in three minutes, and `qwen3:8b` hit Ollama's five-minute load timeout every
  time, on a cold GPU with 15 GB free. Those aborted loads also left VRAM allocated (1.6 GB →
  16 GB over three attempts), which made each retry worse until a reboot cleared it.

  Hiding the GPU from ROCm makes Ollama fall back to the Vulkan backend it already ships, and
  the same `qwen3:8b` loads in **~3 s and runs at 48-81 tok/s**:

  ```bash
  HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve
  ```

  This affects everything on this box that uses Ollama, `~/Code/comfy-agent` included — it
  worked before the update because the older build carried a ROCm 6 runtime.

  On Vulkan, VRAM behaves: an idle `ollama serve` holds none, a resident `qwen3:8b` holds
  ~6.3 GB, and when `keep_alive` expires the card goes back to its ~1.2 GB desktop baseline.
  (Killing Ollama mid-session instead of letting the model expire leaves the buffers allocated
  for a while — that's the driver reclaiming lazily, not a leak.)
- **Ollama isn't a service.** Started by hand, so after a reboot Chat is down until it runs
  again, while the rest of the app is fine. `ollama.service` in this repo fixes that *and* sets
  the two variables above; install it with the three commands in its header comment.
- **Ollama's context is 8192 tokens** here (`num_ctx`). A very long conversation silently loses
  its oldest turns — start a new chat rather than growing one forever.
