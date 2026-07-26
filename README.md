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
./setup.sh      # one-time: app venv + Kokoro + Piper + F5-TTS (see below)
./run.sh        # foreground
./restart.sh    # kill + relaunch in the background (after editing speech.py)
./serve.sh      # one-time: publish on the tailnet over HTTPS via tailscale serve
tail -f speech.log
```

**Setting up a fresh machine.** `./setup.sh` builds the app venv and all three speech engines,
each in its own venv under `~/.local/share`, and fetches their model files. It only needs `uv`.
It's safe to re-run: anything already installed is left alone, and a download whose size is
wrong is rejected rather than left in place looking finished.

```bash
./setup.sh                 # everything
./setup.sh piper kokoro    # just those
./setup.sh --force f5      # reinstall one engine
./setup.sh --check         # report what's installed, change nothing
```

Budget a while for F5 — it pulls several GB of torch (CPU builds on purpose; the default would
fetch CUDA wheels this machine can't use). Kokoro adds ~350 MB of models and each Piper voice
~61 MB. Whisper and F5 fetch their own models on first use, not here. Ollama isn't covered —
that's `ollama.service`.

Port 8443 is used because `443` on this tailnet is already taken by another service.

## Studio (record, transcribe, speak)

**1 · Record or add a clip.** One button records from the mic (Chrome does webm/opus, iOS does
mp4/aac — both are accepted). Everything is normalized through ffmpeg into 24 kHz mono wav in
`clips/`, which is what both Whisper and F5-TTS want internally. The file picker below it covers
the case where the recording was made elsewhere (iOS Voice Memos, exported to Files).

**2 · Speech → text.** faster-whisper on CPU. `small` is the default (a minute of audio in
~10-15 s); `large-v3-turbo` is the accurate, slower option. English and Dutch. The transcript
appears in a copyable block with a **Copy** button, and can be pushed straight into either of
the text fields in panel 3. Transcripts are cached per clip in `clips.json`.

**3 · Text → speech.**

- *Kokoro* — a second or two per sentence. The **▶** beside the picker plays a sample of the
  selected voice, rendered on first press and cached in `samples/`. The language code comes from
  the voice prefix (`bm_george` → `en-gb`), otherwise British voices get phonemized with US rules
  and sound wrong. The pickers list the **American and British voices only**; the other 40-odd
  are still on `/api/voices`, just filtered out of the dropdowns by `GROUPS` in `fillVoices()`.
- *Piper* — the Dutch engine, because Kokoro has no Dutch voice. Five single-speaker voices —
  `alex`, `pim`, `ronnie` (nl_NL), `nathalie`, `rdh` (nl_BE) — plus `nl_NL-mls-medium`, one 76 MB
  model holding **52 readers** from the Dutch MLS corpus, each offered as its own voice
  (`nl_NL-mls-medium-2450`) and all sharing the one loaded model. All *medium* quality, which is
  as good as Piper's Dutch gets: of the 13 `high` voices in the catalogue none is Dutch, so
  choosing among speakers is the way to a better Dutch narrator. Faster than Kokoro — a chat
  reply of 16 s of audio rendered in 2 s. Same ▶ preview button, in Dutch.
- *F5-TTS* — clones a voice. Needs the exact transcript of the reference clip, which is
  auto-filled from Whisper when that clip has already been transcribed. Only clone your own
  voice, or one you have permission to use.

Kokoro and Piper share one voice picker; the engine dropdown just decides which set it shows.
Neither needs naming when you ask for speech — a voice belongs to exactly one engine, so
`/api/speak` works it out from the voice name.

**Saved voices.** Once a reference clip sounds right, "Save this voice for reuse" stores it as a
named preset and the app defaults to it from then on — no re-picking the clip or retyping the
transcript. A preset keeps **its own copy** of the audio (trim already applied) in `presets/`, so
it keeps working after the clip it came from is deleted: clips are scratch space, presets last.

**Quality dial.** F5-TTS sampling steps are selectable: standard (32) or faster (16) — on the same
sentence, ~120 s against 66 s. Generation time scales with output length, so treat "about a minute
a sentence" as a guide rather than a promise. A saved voice does **not** make it faster: F5-TTS is
a zero-shot cloner, re-deriving the voice from the reference every run, and the minutes go into
the output waveform rather than into learning the voice. Presets save setup, the dial saves time.

## Chat (Qwen via Ollama, spoken by Kokoro)

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
  is ~5.2 GB, `qwen3:14b` ~9.3 GB. A **👁** marks a vision model; they answer text fine but trade
  some of that ability for images this UI has no way to send them, so prefer a non-👁 model.
- **Voice** — the same picker as panel 3, ▶ preview included, remembered separately from the
  panel-3 choice.
- **🔊 Speak replies out loud** — off by default. On, the reply is spoken **sentence by
  sentence as it is written**, not rendered in one go at the end: the server cuts the token
  stream at sentence boundaries, hands each piece to Kokoro, and the page plays them back to
  back, so the first audio arrives a few seconds in, while the model is still writing. Rendering
  runs ~2.5× faster than playback, so it stays ahead once it starts. **⏹ Stop speaking** clears
  the queue; asking a new question does too. The 🔊 button on a finished reply re-renders it as
  one piece, and a player is left on every spoken message because a browser can still refuse to
  autoplay.
- **🎤** — tap to talk, tap ⏹ to stop. Goes through the same Whisper path as panel 2 (using the
  model and language chosen there) and lands in the composer. With *Send as soon as I stop
  talking* on, it sends itself, so voice in → voice out needs two taps. A spoken turn is
  dictation, not a clip: it posts to `/api/dictate`, which transcribes and then deletes the
  audio, so nothing accumulates in `clips/` or the Studio list.
- **I speak** — English or Nederlands, per chat. It sets the language 🎤 transcribes with, and
  tells the model to expect that language.
- **What language the reply comes back in is decided by the voice, not by this setting.** The
  chat voice picker offers both engines, and:

  | I speak | voice | reply |
  | --- | --- | --- |
  | English | any Kokoro voice | English |
  | Nederlands | a Kokoro voice | **English** — it can't speak Dutch, so the model is made to answer in English |
  | Nederlands | a Piper voice | **Dutch** — the model is left alone and answers in Dutch |

  So asking in Dutch and being answered in English is still available; it's what you get by
  keeping an English voice selected.
- **System prompt** — per chat. The default asks for short, plain-prose answers, because markdown
  bullets and code fences read terribly out loud.

**Getting an English answer to a Dutch question takes three things together**, and only applies
when an English voice is selected. `qwen3:8b` mirrors the language of the latest user turn and a
system-prompt instruction alone doesn't hold against that, so the app also sends a
`[Reply in English.]` marker on the turn itself and a two-exchange primer answering a Dutch
question in English. Both are attached to what gets **sent**, never to what's stored, so
`chats.json` and the on-screen transcript stay clean.

Chats live in `chats.json` on the server rather than in the browser, so a conversation started
on the PC continues on the phone. The first thing you say names the chat.

**Thinking is disabled.** `qwen3` is a hybrid reasoning model; left alone it emits a `<think>`
block that Kokoro would happily read aloud. The app sends `think: false` — but only to models
whose Ollama capabilities include `thinking`, because `qwen2.5-coder` rejects the flag. Any
`<think>` block that slips through is stripped before the reply is stored.

**Cold loads.** On the Vulkan backend loading `qwen3:8b` takes about 4 seconds; after that it
stays resident for `keep_alive` (5 min) and follow-ups start instantly, and the status line says
which of the two is happening. The per-request `keep_alive` in `speech.py` overrides the server's
`OLLAMA_KEEP_ALIVE`, so change it there; it's short because a resident 8B model holds ~5.2 GB of
the card's 16 GB.

## Books (EPUB narration)

Chapters are extracted the moment an EPUB is added, but nothing is narrated until you ask for
it — a full book is hours of work.

- **Adding** — `epub.py` reads the spine for reading order and the `.ncx` for chapter names,
  then drops covers, colophons, adverts, dedications and part-title pages. The Institute comes
  out as 192 chapters and 20.2 h of audio. A book that packs several chapters into one file and
  addresses each from its table of contents by anchor — `Section0001.xhtml#heading_id_3` — is cut
  at those anchors, so twenty chapters stay twenty chapters. A chapter the TOC names is kept
  however short it is; the length rule that drops part-title pages applies to untitled sections.
- **Narrator** — the same picker as everywhere else, Kokoro English and Piper Dutch. A book
  declaring `nl` gets a Dutch voice by default. Changing it after rendering discards that
  book's audio, so it asks first.
- **Rendering** is per chapter, in **~10-minute parts**, and each part appears as soon as it's
  finished — you can start on part 1 while part 2 is still being made. At the measured 2.4×
  realtime, a 5-minute chapter takes ~2 minutes.
- **Playing** — parts are ordinary opus files played by an `<audio>` element, chaining into
  the next part and then the next chapter. Position is saved server-side every 5 seconds, so
  the phone resumes where the PC left off. Speed is adjustable 0.75–2×.
- **The player floats at the bottom** and lives outside every view, so going back to the
  library, opening a different book or switching to Studio or Chat interrupts neither the audio
  nor the controls. It carries the cover, the chapter and part, the speed, and a way back to the
  book it belongs to, which is not necessarily the one on screen. `×` stops it and records where
  you were. Only the book actually playing is stopped by a re-narration, so changing another
  book's narrator leaves it alone, and reaching the end of what exists re-reads the book once
  before giving up — running out is exactly when a render may have finished the next part.
- **One list, folded.** *Chapters* is a single list at two levels: a book divided into parts —
  The Institute has four — shows those as the outer level, each folding open to its chapters and
  carrying its own **Narrate part** and **Download part**, a far more useful unit than one
  4-minute chapter or the whole 20 hours. Every chapter folds open to its ~10-minute parts, each
  playable the moment it exists, and one left half-made offers to finish itself. What opens
  unasked is the part you're reading and anything rendering or half-done, and it survives the
  4-second refresh. Every chapter has its own **Narrate** button; tapping the row does it too,
  until there's something to play, when tapping plays instead. A part or whole-book run can be
  stopped, a single chapter can't, so anything over ten minutes of work asks first.
- **Narrating now**, above *Whole book*, says which chapter the engine is on and which of its
  parts — "part 1 of 8" from the start, since splitting happens before any audio is made — and
  folds open to what's waiting behind it with a rough total of the work left. It appears only
  when something is happening. The queue is global: renders are serialized *across* books, so
  what's holding this one up can be another book, and the panel names it when it isn't the one
  you're looking at.
- **The book announces itself.** It opens with the title and author — *"Dark Matter" … "by
  Blake Crouch" …* — the way a published audiobook does, and that's also the first thing the
  exported `.m4b` plays. Then before each chapter's prose comes the part's name and the chapter
  number: *"The Night Knocker" … "one" …* and only then the text. The part is spoken only where
  a part actually begins; later chapters in it just get their number. What separates the phrases
  is real silence rather than punctuation, a second or so each, and a chapter closes with a
  longer one so it doesn't run into the next announcement. Off via ⚙, which re-renders the book.

  The number is read out of the heading in **digits or in words**, since a book may name its
  chapters "Chapter 1" or "Chapter One", and goes to the engine as digits so that it comes out in
  whatever language the voice speaks — *"nineteen"* from an English one, *"negentien"* from a
  Dutch one. Same reason the *"by"* in front of the author is the book's own word: *"van"* for a
  Dutch book, since the English one read by a Dutch voice comes out as "bie". A heading that is a
  title rather than a number gets no number: an epigraph shouldn't be introduced as "chapter
  seven".

  A title is written to be read, not heard — *11/22/63: A Novel* has a subtitle no narrator says
  out loud — so ⚙ has a **Say the title as** field that only the announcement uses; the library,
  the chapter marks and the `.m4b` keep the written one. The announcement lives in a chapter's
  first part, and what it says is recorded with the chapter as the engine hears it, so renaming
  the book, changing the narrator or changing how a word is pronounced re-makes that one part
  rather than leaving an opening that no longer matches.
- **Clear narration** in ⚙ throws away the audio and keeps the book, its text and its cover —
  useful after changing something that should be re-spoken.
- **Covers** come from the EPUB, in the library grid, beside the title, in the player, and on
  the phone's lock screen while it plays. Two sizes are derived once with ffmpeg, since the
  original is often ~2 MB and not worth sending a phone repeatedly: **thumb** for everywhere it
  appears small and **full** for the lock screen and the `.m4b`'s artwork. Both cap rather than
  resize, so a smaller cover is left alone rather than blown up, and both keep the book's
  proportions — a tall cover fills a lock screen the way it does in BookPlayer.

  The cover is whichever image the book *declares* — EPUB 3 `properties="cover-image"`, then the
  EPUB 2 `<meta name="cover">` id, then the guide reference — never the first or biggest image,
  since a book's back matter can carry the covers of other novels advertised in it. ⚙ has a
  **Replace the cover** upload for books that declare none.

**Listening away from this PC.** Two buttons under *Whole book*:

- **Narrate the whole book** works through every un-narrated chapter in the background, with
  progress and a stop button. It's hours — 8.4 h for The Institute — so it's meant to run
  overnight. Tapping a single chapter still gets served in between, and stopping lets the
  chapter in flight finish rather than leaving half of one behind. *Narrate part* is the same
  run scoped to one part, counting its progress and hours-left over the part rather than the
  book.
- **Export as audiobook (.m4b)** builds one file from whatever is narrated: chapter markers,
  cover art, title and author, AAC 48 kbps mono. On the phone, tap the download and share it
  into **BookPlayer**, which gives you chapters, sleep timer and position with no PC involved —
  nothing in the export is player-specific. *Whatever is narrated* means every part on disk, not
  only the chapters that finished, and the count of unfinished and un-narrated chapters is
  reported alongside the download. The full book is ~564 MB.

**Why files rather than streaming.** iOS suspends a page's timers when the screen locks, so the
sentence-at-a-time approach Chat uses would stall the moment the phone goes in a pocket. Files
don't: with the phone locked, parts play back to back, each advancing in **under a second**,
which is what makes ~10-minute parts safe.

**Rendering shares the machine, and survives interruption.** `run_lock` is taken per
~600-character chunk and released between them, so a chat reply or a transcription slots in
between rather than waiting out a chapter; one book renders at a time. Between segments a render
checks whether its book still exists and whether the narrator has changed, so a delete or a voice
switch takes effect within one segment and what that render made is thrown away. A render killed
by a restart instead keeps its finished parts: they're real audio, they play, they can be
exported, and re-rendering the chapter reuses them.

Storage is `books.json` for the index and `books/<id>/` for the EPUB, extracted text and audio.
At 32 kbps opus a 20-hour book is ~290 MB of parts, plus the export if you make one. All
gitignored, as is `*.epub`.

## How it fits together

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro_worker   (subprocess, English, resident)
                                              ├── piper_worker    (subprocess, Dutch, resident)
                                              ├── f5-tts CLI      (~/.local/bin/f5-tts)
                                              └── ollama HTTP     (127.0.0.1:11434 → Radeon)
```

**Kokoro is resident, and deliberately not in this app's venv.** The `kokoro-tts` CLI reloads its
325 MB ONNX model on every invocation, ~1.9 s of fixed cost per render against ~0.3 s for a
resident worker — more than the audio itself for a short sentence. `kokoro_worker.py` keeps the
model loaded and takes one JSON request per line over a pipe, launched with **Kokoro's own
interpreter** (`~/.local/share/kokoro-tts/venv/bin/python`) so its 524 MB of ONNX dependencies
stay where they already are.

The worker costs 530 MB–1 GB of RAM while loaded, so it **unloads itself after 10 minutes with no
renders** and starts again on the next one, a restart of about 1.4 s. `KOKORO_IDLE_MINUTES`
changes the window; `0` keeps it resident. Killing it by hand is safe at any time — the next
render restarts it.

Two locks, not one. `run_lock` serializes the CPU model work (Whisper, Kokoro, F5-TTS) because
F5-TTS saturates all 12 cores. Chat gets its own `chat_lock`: Ollama offloads the whole model to
the GPU, so a reply and a Kokoro render use different hardware and are free to overlap — putting
chat behind `run_lock` would make it wait out a two-minute clone for no reason.

Both TTS engines have their own venvs, so the app borrows their interpreters rather than
duplicating their dependencies — F5-TTS via its CLI per render, Kokoro via the resident worker
above. Whisper is the exception: it's the most-used path, so `faster-whisper` lives in this app's
venv with the model resident between requests.

Long jobs are threads writing into a `jobs` dict that the page polls at `/api/status/<id>`, the
same pattern as `~/Code/comfy-webui`.

## Modules

One file per feature, plus a thin `core.py` holding what they share: the Flask app, the job
table, the locks that keep one model on the GPU at a time, and two file helpers. It exists so
`books.py` and `chat.py` can both reach the app without importing each other.

`write_json`, one of those two helpers, writes every index (`clips.json`, `presets.json`,
`chats.json`, `books.json`) to a temp file and renames it, so a read that lands mid-write gets a
whole file rather than half of one — a whole-book render rewrites the book index after every
chapter while the page is polling it.

Routes are registered by importing the modules for their side effect — `speech.py` imports
all five and each decorates the shared `app`. That's the one fragile part: a module that stops
being imported takes its endpoints with it and nothing else notices, so `tests/test_routes.py`
writes the whole URL table out and fails on any difference.

Each module owns the storage it's responsible for — `books.py` holds `BOOKS_DIR` and
`BOOKS_FILE` rather than taking them from core. That's what lets a test point the whole book
layer at a tmpdir by patching two names on one module.

The import order is `core` → `textprep`/`media` → `clips` → `tts` → `stt`/`chat`/`books`, and
nothing imports upwards. Functions are grouped by feature rather than wrapped in classes:
this is functions over a JSON document and a few locks, and there's no object model in it
straining to get out.

## Layout

```
setup.sh      one-time install of the app venv and all three speech engines
speech.py     entry point (port 8600). Imports the feature modules for the routes they
              register, serves the page, starts the worker reaper. Not named app.py, so
              restart.sh can't collide with comfy-webui's.
core.py       the Flask app, the job table, the locks, write_json/safe_path
textprep.py   prose -> speakable text, and cutting it into sentences
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
tests/        pytest suite; see Tests below
pytest.ini    test config (rootdir on the import path)
requirements-test.txt   what CI installs — flask, bs4, lxml, pytest, and nothing heavier
.github/workflows/tests.yml   runs the suite on push and pull request
CLAUDE.md     house rules: tests come with the change, what belongs in this README,
              no copyrighted text in the repo
LICENSE       MIT
clips/        normalized input clips (gitignored)
presets/      saved voices — own copy of the reference audio (gitignored)
samples/      cached one-line voice previews, one wav per voice (gitignored)
books/        per book: the epub, extracted chapter text, rendered audio (gitignored)
books.json    book index: chapters, narrator, render state, listening position (gitignored)
outputs/      generated audio (gitignored)
clips.json    clip index + cached transcripts (gitignored)
presets.json  saved voices: name, reference transcript, source clip (gitignored)
chats.json    chat transcripts, per-chat model and system prompt (gitignored)
```

## Tests

```bash
uv pip install --python .venv/bin/python pytest    # once
.venv/bin/python -m pytest                         # ~15 s
```

Also on every push and pull request, via `.github/workflows/tests.yml`.

**What's covered.** Every module, ~340 tests. The books state machine, which is where the bugs
actually live: a render cancelled under itself, what survives a restart, which chapters an export
takes, how a part run scopes its progress, the queue. The pure functions — reading a chapter
number out of a heading, cutting text into segments, chunks and sentences, what gets announced
before the prose. The stores behind clips, presets and chats. And the HTTP contracts, including
the cache headers on narration audio.

Two things get more attention than their size suggests: `safe_path`, the app's only security
boundary, against climbing out, absolute paths, outward symlinks and shared-prefix siblings; and
the arrangement that gets an English answer out of a Dutch question, whose central rule is
invisible from the outside — the marker and the primer must never reach `chats.json`.

**What isn't.** Anything you have to hear. Whether a pause is long enough, whether a voice reads
a name right, whether the lock screen looks right — no assertion tells you that. The external
engines aren't exercised either; each is stubbed at its boundary (`_render_segment`, `run_stt`,
`worker_call`, `ollama_models`) and everything above it is real, so what's tested is which model
gets asked for and what happens to the answer. `tests/test_export.py` is the exception and runs
ffmpeg for real, reading the chapter marks back out of a finished `.m4b` — the only way to know
they line up with the audio rather than merely being written.

`tests/test_frontend.py` is structural, not behavioural: every `$("#id")` resolves, no element is
left unreferenced, tags balance, ids are unique, and the inline script parses under `node --check`.
One file with no build step means nothing else catches a dangling reference before the phone does.

Tests needing a tool beyond python skip when it isn't installed, which is right on a laptop and
wrong in CI, where skipping is how a suite passes without having run. `STRICT_TESTS=1`, which CI
sets, refuses the run instead and names what's missing.

**Isolation.** Each module keeps its storage paths in module globals, so tests point all nine —
books, clips, presets, outputs, samples, chats — at a tmpdir, autouse, and a second fixture fails
any test that changed real storage. A third refuses to start a speech engine, since
`kokoro_voices()` reads like a list lookup and is actually a subprocess launch plus a cache that
would leak into every test after it. Don't call `monkeypatch.undo()` in a test: it reverts those
redirects along with everything else, and the rest of the test then runs against your own library.

## Gotchas

- **No mic on plain http** — use the HTTPS URL. The UI says so if it detects an insecure context.
- **Whisper model downloads** happen on first use (`small` ≈ 500 MB, `large-v3-turbo` ≈ 1.6 GB)
  into `~/.cache/huggingface`; the first transcription after a switch is slower.
- **F5-TTS needs the reference transcript.** If it's blank the CLI would download and run its
  own Whisper, so the app rejects the request instead.
- **Restarting drops running jobs** — the `jobs` dict is in memory. The page says so if you poll
  a job the server no longer knows. Chat *messages* survive (they're in `chats.json`); only a
  reply still being written is lost.
- **Ollama picks the wrong GPU backend.** Ollama ships only a ROCm 7.2 runtime, and ROCm 7
  dropped consumer RDNA2 — which is what the RX 6900 XT (gfx1030) is. Left alone Ollama chooses
  ROCm and the upload to VRAM crawls: `qwen3:8b` hits the five-minute load timeout with 15 GB
  free, and the aborted loads keep VRAM allocated until a reboot. Hiding the GPU from ROCm makes
  it fall back to the Vulkan backend it already ships, where the same model loads in **~3 s and
  runs at 48-81 tok/s** and VRAM behaves — nothing held when idle, ~6.3 GB resident, back to the
  ~1.2 GB desktop baseline when `keep_alive` expires:

  ```bash
  HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve
  ```

  This affects everything on this box that uses Ollama, `~/Code/comfy-agent` included.
- **Ollama isn't a service.** Started by hand, so after a reboot Chat is down until it runs
  again, while the rest of the app is fine. `ollama.service` in this repo fixes that *and* sets
  the two variables above; install it with the three commands in its header comment.
- **Ollama's context is 8192 tokens** here (`num_ctx`). A very long conversation silently loses
  its oldest turns — start a new chat rather than growing one forever.
- **Spoken replies leave wavs in `outputs/`**, one per sentence-chunk rather than one per
  reply, so a talkative session accumulates quickly (~190 KB per chunk). They're gitignored
  and safe to delete wholesale.
- **Fixing a mispronunciation means respelling it.** `RESPELL` in `textprep.py` maps a word to
  something the engine says correctly — `movies` → `movees`, because espeak clips the `-ies` to
  "movis". Kokoro's documented `[word](/phonemes/)` override belongs to the KPipeline package and
  not to `kokoro_onnx`, which reads the markup itself out loud, so respelling is the whole
  toolkit.
- **A slash between numbers isn't a sound.** `11/22/63` read as written comes out *"eleven slash
  twenty-two slash sixty-three"*, so the slash becomes the comma that is the beat between the
  groups — `11, 22, 63` — and a leading zero comes off, `02` being read *"zero two"*. The digits
  themselves are left to the engine, which says *"elf, tweeëntwintig"* for a Dutch voice and
  *"eleven, twenty-two"* for an English one, and reads a four-digit group as a year in both;
  spelling them out here would mean spelling them out in one language. Which group is the month
  is never guessed at — `10/7` is October 7th in an American book and July 10th in a Dutch one —
  and two single digits are left alone, `1/2` being a fraction far more often than a date. Prose
  as much as titles.

  A hyphenated date gets the same treatment but needs a narrower rule, since between numbers a
  hyphen is usually a range: `1914-1918`, `pages 10-20`, `the 2020-21 season`. Only the two forms
  carrying a four-digit year are read as a date — `10-02-1986` and `1986-02-10` — and every other
  hyphen is left where it is.
- **Titles are written out before they're spoken.** `Mr.` reaches the engine as `Mister`, since
  the full stop otherwise reads as a sentence break and drops a pause between the title and the
  name. Same for `Mrs.`, `Ms.`, `Dr.`, `Prof.`, and for `Jr.`, `Sr.`, `vs.`, `etc.`, `e.g.`,
  `i.e.`, `approx.`, which keep the stop only when it also ends the sentence. `St.` is left alone:
  it's Saint before a name and Street after one, and nothing here can tell which.
- **Kokoro has no Dutch voice** — its 54 cover nine languages and Dutch isn't one, which is why
  Piper is here. It *can* be forced at Dutch through espeak-ng, but the result is an American
  accent reading Dutch spelling. Not exposed in the UI; use a Piper voice.
- **Six of Piper's ten Dutch models are installed**, which is 57 selectable voices once
  `nl_NL-mls-medium`'s 52 speakers are counted. The rest are `x_low`/`low` variants of speakers
  already here. Add more by dropping the `.onnx` and `.onnx.json` from
  huggingface.co/rhasspy/piper-voices into `~/.local/share/piper-tts/voices` — picked up on the
  next restart, no code change, and a multi-speaker model is expanded automatically.

## License

MIT, see [LICENSE](LICENSE). The speech engines it drives are separately licensed, and the books
in `books/` are bought copies that stay out of the repository.
