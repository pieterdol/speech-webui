# Local Speech Studio

A phone-friendly web front-end for the local speech tools on this PC. Everything runs
locally — speech on the CPU, the language model on the Radeon; nothing is sent to a cloud
service. The header switches between three modes.

**Studio** — the three speech panels. Record or upload a clip, get the text out of it with
Whisper, and turn text back into speech: Kokoro's built-in voices for English, Piper for Dutch,
or F5-TTS to clone a voice from a reference clip.

**Chat** — talk to a local Qwen model through Ollama, by typing or by voice, and have its reply
read back to you sentence by sentence as it's written.

**Books** — add an EPUB, pick a narrator, and listen on your phone. Chapters are narrated in
~10-minute parts, your position follows you between PC and phone, and what's narrated can be
exported as an `.m4b` audiobook.

The detail lives in [docs/](docs): [books.md](docs/books.md) for narration and export,
[architecture.md](docs/architecture.md) for how it's put together,
[gotchas.md](docs/gotchas.md) for the constraints of this machine and its engines, and
[testing.md](docs/testing.md) for what the suite covers.

## URLs

| Where | URL |
| --- | --- |
| Phone / tailnet | `https://your-machine.your-tailnet.ts.net:8443` |
| This PC | `http://127.0.0.1:8600` |

**The HTTPS URL matters.** Safari blocks the microphone (`getUserMedia`) and the clipboard outside a
secure context, so over plain http the record button and the Copy button don't work — only the file
picker does. The UI says so if it detects an insecure context. `localhost` counts as secure, which is
why the PC URL is fine.

## Running it

```bash
./setup.sh      # one-time: app venv + Kokoro + Piper + F5-TTS
./run.sh        # foreground
./restart.sh    # kill + relaunch in the background (after editing speech.py)
./serve.sh      # one-time: publish on the tailnet over HTTPS via tailscale serve
tail -f speech.log
```

`./setup.sh` builds the app venv and all three speech engines, each in its own venv under
`~/.local/share`, and fetches their model files. It only needs `uv`. It's safe to re-run: anything
already installed is left alone, and a download whose size is wrong is rejected rather than left in
place looking finished.

```bash
./setup.sh                 # everything
./setup.sh piper kokoro    # just those
./setup.sh --force f5      # reinstall one engine
./setup.sh --check         # report what's installed, change nothing
```

Budget a while for F5 — it pulls several GB of torch (CPU builds on purpose; the default would fetch
CUDA wheels this machine can't use). Whisper and F5 fetch their own models on first use, not here.
Ollama isn't covered — that's `ollama.service`.

Port 8443 is used because `443` on this tailnet is already taken by another service.

## Studio (record, transcribe, speak)

**1 · Record or add a clip.** One button records from the mic (Chrome does webm/opus, iOS does
mp4/aac — both are accepted), normalized through ffmpeg into 24 kHz mono wav in `clips/`, which is
what both Whisper and F5-TTS want. The file picker below it covers a recording made elsewhere.

**2 · Speech → text.** faster-whisper on CPU. `small` is the default (a minute of audio in ~10–15 s);
`large-v3-turbo` is the accurate, slower option. English and Dutch. The transcript appears in a
copyable block and can be pushed straight into either of the text fields in panel 3. Transcripts are
cached per clip in `clips.json`.

**3 · Text → speech**, one of three engines:

- *Kokoro* — English, a second or two per sentence. The **▶** beside the picker plays a sample of the
  selected voice, cached in `samples/`.
- *Piper* — the Dutch engine, because [Kokoro has no Dutch voice](docs/gotchas.md). Five voices, same
  ▶ preview, and faster than Kokoro.
- *F5-TTS* — clones a voice from a reference clip. It **needs the exact transcript** of that clip,
  auto-filled from Whisper when the clip has already been transcribed. Only clone your own voice, or
  one you have permission to use.

**Saved voices.** Once a reference clip sounds right, "Save this voice for reuse" stores it as a named
preset and the app defaults to it from then on. A preset keeps **its own copy** of the audio (trim
already applied) in `presets/`, so it keeps working after the clip it came from is deleted: clips are
scratch space, presets last.

**Quality dial.** F5-TTS sampling steps are selectable: standard (32) or faster (16), roughly twice
the speed. Generation time scales with output length, so treat "about a minute a sentence" as a guide
rather than a promise. A saved voice does **not** make it faster: F5-TTS is a zero-shot cloner,
re-deriving the voice from the reference every run. Presets save setup, the dial saves time.

Detail: [docs/architecture.md](docs/architecture.md) for the engines,
[docs/gotchas.md](docs/gotchas.md) for the voices.

## Chat (Qwen via Ollama, spoken by Kokoro)

**Ollama must be running** — the app talks to `http://127.0.0.1:11434` and does not start it:

```bash
HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve   # or OLLAMA_URL if it's elsewhere
```

**Those two variables are not optional on this PC**: without them Ollama picks a ROCm runtime this
GPU can't use and a model load times out ([why](docs/gotchas.md)). `ollama.service` in this repo is a
systemd user unit that sets them for you. If Ollama isn't up the chat panel says so instead of
failing silently, and everything else in the app keeps working.

- **Model** — every model installed in Ollama, Qwen first (`qwen3:8b` is the default). Sizes are
  shown because they have to fit in the 16 GB alongside anything ComfyUI has loaded. A **👁** marks a
  vision model; they answer text fine but this UI has no way to send them images, so prefer a non-👁
  model.
- **Voice** — the same picker as panel 3, ▶ preview included, remembered separately.
- **🔊 Speak replies out loud** — off by default. On, the reply is spoken **sentence by sentence as it
  is written**: the server cuts the token stream at sentence boundaries, hands each piece to Kokoro,
  and the page plays them back to back, so the first audio arrives a few seconds in. **⏹ Stop
  speaking** clears the queue; asking a new question does too. The 🔊 button on a finished reply
  re-renders it as one piece, and a player is left on every spoken message because a browser can
  still refuse to autoplay.
- **🎤** — tap to talk, tap ⏹ to stop. Goes through the same Whisper path as panel 2 and lands in the
  composer. With *Send as soon as I stop talking* on, it sends itself, so voice in → voice out needs
  two taps. A spoken turn is dictation, not a clip: it's transcribed and the audio deleted, so
  nothing accumulates in `clips/`.
- **I speak** — English or Nederlands, per chat. It sets the language 🎤 transcribes with, and tells
  the model to expect that language.
- **System prompt** — per chat. The default asks for short, plain-prose answers, because markdown
  bullets and code fences read terribly out loud.

**What language the reply comes back in is decided by the voice, not by *I speak*.** The chat voice
picker offers both engines:

| I speak | voice | reply |
| --- | --- | --- |
| English | any Kokoro voice | English |
| Nederlands | a Kokoro voice | **English** — it can't speak Dutch, so the model is made to answer in English |
| Nederlands | a Piper voice | **Dutch** — the model is left alone and answers in Dutch |

So asking in Dutch and being answered in English is what you get by keeping an English voice
selected. Chats live in `chats.json` on the server rather than in the browser, so a conversation
started on the PC continues on the phone; the first thing you say names the chat.

Detail: [docs/architecture.md](docs/architecture.md).

## Books (EPUB narration)

Chapters are extracted the moment an EPUB is added, but nothing is narrated until you ask for it — a
full book is hours of work.

- **Adding** — the spine gives reading order and the `.ncx` the chapter names; covers, colophons,
  adverts and part-title pages are dropped, a file holding several chapters is cut at the anchors its
  contents addresses, and a title on a page of its own is folded into the chapter it names.
- **Left out of the narration** lists what extraction dropped and why. **⤒** puts a section's words
  into *Read this at the start*; **＋** puts it back as a chapter of its own, where the book has it.
- **⊘ leaves a chapter out** and **↩** puts it back, for apparatus no pattern can tell from prose.
  **↻** narrates a chapter again from nothing.
- **🎧 hears how a chapter will start** — its announcement and the first ~600 characters, about a
  minute of audio — before committing hours to the wrong narrator, title or pronunciation.
- **Narrator** — the same picker as everywhere else, Kokoro English and Piper Dutch. A book declaring
  `nl` gets a Dutch voice by default. Changing it after rendering discards that book's audio, so it
  asks first.
- **Rendering** is per chapter, in **~10-minute parts**, each appearing as soon as it's finished.
  Renders are serialized across every book, and a tap says whether it started or queued.
- **Narrate the whole book** or **Narrate part** works through everything un-narrated in the
  background. It's hours, so it says the time it will finish, not only how long it takes, and a run
  can be widened while it runs.
- **Playing** — parts are ordinary opus files, chaining into the next part and then the next chapter.
  Speed is 0.75× to 2×, **↺10 / 30↻** skip across part boundaries, and the lock screen's own buttons
  move by the same amounts. Position is saved server-side every 5 seconds, so the phone resumes where
  the PC left off.
- **The player floats at the bottom** and lives outside every view, so opening another book or
  switching to Studio interrupts neither the audio nor the controls. **▶ Resume** sits at the top of a
  book you've started and on its cover in the library.
- **The book announces itself** with its title and author, and each chapter with its part and its
  number or title — real silence between the phrases rather than punctuation. A chapter number is
  read whether the book writes it in digits, words or roman numerals. ⚙ has **Read this at the
  start** for a dedication and **Say the title as** for a title with a subtitle no narrator says out
  loud.
- **Say these words differently** is the book's own pronunciation list, with ▶ to hear a respelling
  in this book's voice and a search for how the book actually spells a name. One save applies the
  lot, and re-narrates only the parts that said it the old way.
- **Export as audiobook (.m4b)** builds one file from whatever is narrated: chapter markers, cover
  art, title and author, AAC 48 kbps mono. Every export the book still has is listed with what went
  into it. On the phone it's handed to the iOS share sheet rather than downloaded, which passes it
  straight to **BookPlayer** ([why](docs/gotchas.md)).
- **Covers** come from the EPUB — the image the book declares, not the first one it happens to
  contain — and appear in the library, the player and the phone's lock screen. ⚙ can replace one.
- **Clear narration** throws away the audio and keeps the book, its text and its cover. The EPUB
  itself can be downloaded back off the machine from ⚙.

`books.json` holds the index and `books/<id>/` the EPUB, extracted text and audio. At 32 kbps opus a
20-hour book is ~290 MB of parts, plus the export if you make one. All gitignored, as is `*.epub`.

Detail: [docs/books.md](docs/books.md).

## How it fits together

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro_worker   (subprocess, English, resident)
                                              ├── piper_worker    (subprocess, Dutch, resident)
                                              ├── f5-tts CLI      (~/.local/bin/f5-tts)
                                              └── ollama HTTP     (127.0.0.1:11434 → Radeon)
```

Both TTS engines have their own venvs and the app borrows their interpreters rather than duplicating
their dependencies. Kokoro runs as a resident worker, since reloading its 325 MB ONNX model per
render costs more than the audio itself for a short sentence; it unloads itself after ten idle
minutes. Whisper is the exception and lives in this app's venv, model resident between requests.

Two locks, not one. `run_lock` serializes the CPU model work (Whisper, Kokoro, F5-TTS) because F5
saturates all 12 cores; Chat gets its own `chat_lock`, since Ollama works on the GPU and a reply can
overlap a render for free. Long jobs are threads writing into a `jobs` dict that the page polls.

Detail: [docs/architecture.md](docs/architecture.md).

## Modules

One file per feature, plus a thin `core.py` holding what they share, so `books.py` and `chat.py` can
both reach the Flask app without importing each other.

```
setup.sh      one-time install of the app venv and all three speech engines
speech.py     entry point (port 8600); imports the feature modules for the routes they register
core.py       the Flask app, the job table, the locks, write_json/safe_path/log_transfer
textprep.py   prose -> speakable text, respellings, and cutting it into sentences
media.py      ffmpeg and ffprobe helpers
clips.py      recorded/uploaded audio, transcripts, voice presets
stt.py        faster-whisper
tts.py        Kokoro and Piper resident workers, F5 cloning, /api/speak
chat.py       Ollama, chats.json, replies spoken as they stream
books.py      EPUB narration: index, rendering, queue, .m4b export
epub.py       EPUB -> chapters of narratable prose
kokoro_worker.py  resident Kokoro process (English), run with Kokoro's venv interpreter
piper_worker.py   resident Piper process (Dutch), run with Piper's venv interpreter
index.html    the whole UI — one file, no build step
```

Routes are registered by import side effect, which is the one fragile part: a module that stops being
imported takes its endpoints with it, so `tests/test_routes.py` writes the whole URL table out and
fails on any difference. Each module owns its own storage paths, which is what lets a test point a
whole layer at a tmpdir.

Detail: [docs/architecture.md](docs/architecture.md).

## Layout

```
docs/         books.md, architecture.md, gotchas.md, testing.md
tests/        pytest suite
pytest.ini    test config (rootdir on the import path)
requirements-test.txt   what CI installs — flask, bs4, lxml, pytest, and nothing heavier
.github/workflows/tests.yml   runs the suite on push and pull request
CLAUDE.md     house rules: tests come with the change, what belongs in the README and what in
              docs/, no copyrighted text in the repo
LICENSE       MIT
clips/        normalized input clips (gitignored)
presets/      saved voices — own copy of the reference audio (gitignored)
samples/      cached one-line voice previews, one wav per voice (gitignored)
books/        per book: the epub, extracted chapter text, rendered audio (gitignored)
outputs/      generated audio (gitignored)
books.json    book index: chapters, narrator, render state, listening position (gitignored)
clips.json    clip index + cached transcripts (gitignored)
presets.json  saved voices: name, reference transcript, source clip (gitignored)
chats.json    chat transcripts, per-chat model and system prompt (gitignored)
```

## Tests

```bash
uv pip install --python .venv/bin/python pytest    # once
.venv/bin/python -m pytest                         # ~40 s
```

Also on every push and pull request. Every module is covered, weighted towards the books state
machine and the pure text functions; the external engines are stubbed at their boundaries, so what's
tested is which model gets asked for and what happens to the answer. Anything you have to hear isn't
covered, and can't be. `STRICT_TESTS=1`, which CI sets, refuses to skip a test whose tool is missing
rather than passing without having run it.

Detail: [docs/testing.md](docs/testing.md).

## Gotchas

- **No mic on plain http** — use the HTTPS URL.
- **Ollama needs two environment variables** on this PC, isn't started by the app, and isn't a
  service; after a reboot Chat is down until it runs again.
- **A long chat silently loses its oldest turns** at 8192 tokens — start a new one.
- **Restarting drops running jobs** — the `jobs` dict is in memory. Chat *messages* survive; only a
  reply still being written is lost.
- **On the phone an audiobook is shared, not downloaded** — a download can't finish inside a
  home-screen app.
- **F5-TTS needs the reference transcript**; a blank one is rejected.
- **Kokoro has no Dutch voice** — use a Piper voice.
- **Whisper models download on first use**, so the first transcription after a switch is slower.
- **Spoken replies leave wavs in `outputs/`**, one per sentence-chunk; safe to delete wholesale.
- **Fixing a mispronunciation means respelling it**, globally in `textprep.py` or per book in ⚙.

Detail, with the reasoning that stops each one being undone: [docs/gotchas.md](docs/gotchas.md).

## License

MIT, see [LICENSE](LICENSE). The speech engines it drives are separately licensed, and the books
in `books/` are bought copies that stay out of the repository.
