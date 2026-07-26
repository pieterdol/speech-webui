# Local Speech Studio

A phone-friendly web front-end for the local speech tools on this PC. Record (or upload) a
clip, get the text out of it, and turn text back into speech — either with Kokoro's built-in
voices or by cloning a reference clip with F5-TTS. There's also a **Chat** mode: talk to a
local Qwen model through Ollama and have its replies read back to you in a Kokoro voice.
Everything runs locally — speech on the CPU, the language model on the Radeon; nothing is
sent to a cloud service.

The header has three modes: **Studio** (the three speech panels), **Chat**, and **Books**.

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

Budget a while for F5 — it pulls several GB of torch (CPU builds on purpose; the default
would fetch CUDA wheels this machine can't use). Kokoro adds ~350 MB of models and each Piper
voice ~61 MB. Whisper and F5 download their own models on first use, not here. Ollama is not
covered — that's `ollama.service`.

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

- *Kokoro* — a second or two per sentence. The **▶** beside the picker plays a sample of the
  selected voice ("Hello! How can I assist you today?"), rendered on first press and cached in
  `samples/` after that. The language code is derived from the voice prefix (`bm_george` →
  `en-gb`), otherwise British voices get phonemized with US rules and sound wrong.

  The pickers list the **American and British voices only**. Kokoro ships 54, including
  Spanish, French, Hindi, Italian, Portuguese, Japanese and Mandarin; `/api/voices` still
  returns all of them, they're just filtered out of the dropdowns. Add their prefixes back to
  `GROUPS` in `fillVoices()` to bring them in.
- *Piper* — the Dutch engine, because Kokoro has no Dutch voice. Five voices: `alex`, `pim` and
  `ronnie` (nl_NL), `nathalie` and `rdh` (nl_BE), all *medium* quality, ~61 MB each in
  `~/.local/share/piper-tts/voices`. Faster than Kokoro — a chat reply of 16 s of audio
  rendered in 2 s. Same ▶ preview button, in Dutch.
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
  is ~5.2 GB, `qwen3:14b` ~9.3 GB. A **👁** marks a vision model (Ollama's `vision` capability,
  looked up once per model and cached). They answer text perfectly well — tested, all three
  reply — but they trade some text ability for images this UI has no way to send them, and
  `moondream` is 1B and shows it. For plain chat, prefer a non-👁 model.
- **Voice** — the same picker as panel 3, ▶ preview included, remembered separately from the
  panel-3 choice.
- **🔊 Speak replies out loud** — off by default. On, the reply is spoken **sentence by
  sentence as it is written**, not rendered in one go at the end: the server cuts the token
  stream at sentence boundaries, hands each piece to Kokoro, and the page plays them back to
  back. On a twelve-sentence answer the first audio arrived at 4.6 s — while the model was
  still writing, and against ~45 s of silence for a single whole-reply render. Rendering runs
  ~2.5× faster than playback, so it stays ahead once it starts.

  Chunks are held to 45 characters (20 for the first, which sets the perceived wait). Below
  roughly half a second of audio a render takes longer than the playback it has to cover, and
  the speech develops gaps. **⏹ Stop speaking** clears the queue; asking a new question does
  too. The 🔊 button on a finished reply re-renders it as one piece, and a player is left on
  every spoken message because a browser can still refuse to autoplay.
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

**Getting an English answer to a Dutch question takes more than asking** — this applies only
when an English voice is selected. A system-prompt
instruction alone did not hold: `qwen3:8b` mirrors the language of the latest user turn and
answered in Dutch anyway, most stubbornly on Dutch subject matter. What works, measured 16/16
against 3/5 for the instruction alone, is three things together — the note in the system
prompt, a `[Reply in English.]` marker on the turn itself, and a two-exchange primer showing a
Dutch question answered in English (the second example deliberately being a factual question
about the Netherlands, which is where it slipped). The marker and the primer are attached to
what gets **sent**, never to what's stored, so `chats.json` and the on-screen transcript stay
clean. Getting turn one right matters most: one Dutch reply in the history and every later
turn copies it.

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

## 5 · Books (EPUB narration)

Add an EPUB, pick a narrator, and listen on your phone. Chapters are extracted on upload but
nothing is narrated until you ask for it — a full book is hours of work.

- **Adding** — `epub.py` reads the spine for reading order and the `.ncx` for chapter names,
  then drops covers, colophons, adverts, dedications and part-title pages. The Institute comes
  out as 192 chapters from 213 spine documents, 177,350 words, 20.2 h of audio.
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
  library, opening a different book or switching to Studio or Chat leaves it where it is —
  the audio was never interrupted by any of those, and now the controls aren't either. It
  carries the cover, the chapter and part, the speed, and a way back to the book it belongs
  to, which is not necessarily the one on screen. `×` stops it and records where you were.

  Which means what's playing is tracked separately from what's open: the book the reader has
  loaded is nulled when you leave it and replaced when you open another, while the player
  needs its own book for the whole time it's sounding — for the position it saves, the
  chapter it advances to, and what it puts on the lock screen. Only the narration of the book
  actually playing stops the player, so changing another book's narrator leaves it alone.
  When it reaches the end of what exists it re-reads the book once before giving up, since
  running out is exactly when a render may have just finished the next part.
- **One list, folded.** *Chapters* is a single list at two levels. A book divided into parts —
  The Institute has four — shows them as the outer level, each folding open to its chapters
  and carrying its own **Narrate part** and **Download part**, which is a far more useful unit
  than one 4-minute chapter or the whole 20 hours; a book without parts is the same list with
  that level missing. Every chapter then folds open to its ~10-minute parts, each playable the
  moment it exists — a chapter three parts into a six-part render is already worth listening
  to, and one left half-made offers to finish itself. What opens unasked is the part you're
  reading and anything rendering or half-done; the state survives the 4-second refresh, so a
  panel you're reading doesn't snap shut while the book narrates. Every chapter carries its
  own **Narrate** button: tapping the row does it too, but only while the chapter has nothing
  to play — once it has, tapping plays instead — and a book with no parts has no *Narrate
  part* either, so otherwise there'd be nothing visible to press. A single chapter has no
  stop button, unlike a part or whole-book run, so anything over ten minutes of work asks
  first.
- **Narrating now**, above *Whole book*, says which chapter the engine is on and which of its
  parts, and folds open to what's behind it with a rough total of the work left. It appears
  only when something is happening. `render_lock` serializes renders but a lock says nothing
  about who is holding it or who is stacked up behind them, so `render_slot` books each render
  in as waiting and then as current, and clears it however the render ends — including the
  early return for a chapter that turned out to be narrated already. A whole-book run doesn't
  queue its chapters up front, it takes the next pending one each time round the loop, so the
  rest of it is derived from the book rather than from the waiting list. The queue is global:
  renders are serialized *across* books, so what's holding this one up can be another book,
  and the panel names it when it isn't the one you're looking at. Two threads can be waiting
  on the same chapter — you tapped it and the bulk run reached it too — and that's one line,
  since the second finds it already made and returns.

  A chapter is split into parts before its state is published, not while it renders, so the
  count is known before the first part exists and the panel can say "part 1 of 8" from the
  start. Splitting is pure text work, so there's nothing to wait for.
- **The book announces itself.** It opens with the title and author — *"Dark Matter" … "by
  Blake Crouch" …* — the way a published audiobook does, and that's also the first thing the
  exported `.m4b` plays. Then before each chapter's prose you hear the part's name and the
  chapter number: *"The Night Knocker" … "one" …* and only then the text. The part is spoken
  only where a part actually begins; later chapters in it just get their number. The pauses
  are real silence (0.7/1.6 s for the opening, 1.2 s and 0.9 s for part and chapter) added
  with ffmpeg's `apad` on the announcement clip itself — a full stop buys about a third of a
  second, which doesn't read as a new chapter, and a separately generated silence file would
  risk disagreeing with the engine's sample rate (Kokoro 24 kHz, Piper 22.05) and breaking
  the concatenation. Numbers are spoken as words so "21" can't come out as a year, and a
  chapter closes with 1.8 s of silence so it doesn't run straight into the next announcement.
  Off via ⚙, which re-renders the book.

  The number is read out of the heading in **digits or in words**, since a book may name its
  chapters "Chapter 1" or "Chapter One" — Dark Matter spells them out, The Institute doesn't.
  A heading that is a title rather than a number gets none: an epigraph shouldn't be
  introduced as "chapter seven". The announcement lives in a chapter's first part, and the
  phrases used are recorded with the chapter, so resuming a chapter that was interrupted
  before the wording changed re-makes that one part instead of keeping an opening that no
  longer matches.
- **Clear narration** in ⚙ throws away the audio and keeps the book, its text and its cover —
  useful after changing something that should be re-spoken.
- **Covers** come from the EPUB, in the library grid, beside the title, in the player, and on
  the phone's lock screen while it plays. Two sizes are derived once with ffmpeg, because the
  original is often ~2 MB and not worth sending a phone repeatedly: **thumb** for everywhere
  it appears small — no wider than 104 px, but on a 3× screen — and **full** for the lock
  screen and the `.m4b`'s artwork, where iOS draws it around 1050 px across.

  Both cap rather than resize, `min(400,iw)` and `min(1000,iw)`, so a book whose own cover is
  smaller is left alone instead of being blown up — of three books here the sources are 825,
  986 and 1325 px wide, and only the last is reduced. Both keep the book's proportions: a
  phone lays cover art out at whatever shape it's handed, so a tall cover fills the lock
  screen the way it does in BookPlayer, and squaring one off only adds bars. The dimensions
  are measured in the browser and go out with the artwork, since `sizes` is the shape the OS
  lays it out by and has to match the file — a 600×906 cover announced as 512×512 gets bars.
  ⚙ has a **Replace the cover** upload for books that declare none.

  The cover is whichever image the book *declares* — EPUB 3 `properties="cover-image"`, then
  the EPUB 2 `<meta name="cover">` id, then the guide reference. Never the first or biggest
  image: The Institute ships six `buylink_*_cover.jpg` files, which are the covers of other
  novels advertised in the back matter. Books added before covers existed get one made on
  demand from the stored EPUB, so nothing needs re-adding.

**Why files rather than streaming.** iOS suspends a page's timers when the screen locks, so
the sentence-at-a-time approach Chat uses would stall the moment the phone goes in a pocket.
Measured instead: with the phone locked, three files played back to back, each advancing in
**under a second**, and the page's own beacons still arrived — so iOS kept the JavaScript
running as well as the audio. That result is what makes ~10-minute parts safe; without it the
design would need whole chapters in single files.

**Rendering doesn't monopolise the machine.** `run_lock` is taken per ~600-character chunk and
released between them, so a chat reply or a transcription slots in between rather than waiting
out a chapter. One book renders at a time.

**A render can be cancelled under itself.** Between every segment it checks whether its book
still exists and whether the narrator has changed, so deleting a book or switching its voice
takes effect within one segment rather than at the end of the chapter. A cancelled render
throws away what it made: a narrator change puts the chapter back to *pending*, and a deleted
book takes the whole directory, since a render in flight keeps recreating the directory the
delete removed. ffmpeg failing with "No such file or directory" in that window is the delete
working, so it isn't recorded as a chapter error.

**A killed render keeps what it made.** The workers live in the Flask process, so a restart —
or shutting the PC down overnight mid-chapter — kills them, and anything still marked
*rendering* on the way back up is a leftover. It goes back to *pending*, but the parts it had
already finished stay listed as long as the files are on disk: they're real audio, they play,
they can be exported, and re-rendering the chapter reuses them. The list is rebuilt by reading
the audio directory rather than trusting the index, because a render empties it before it
starts refilling it. It stops at the first gap, since playback walks the parts in order, and
drops a trailing file ffprobe can't read a duration out of — that one was being written when
the process died.

**Listening away from this PC.** Two buttons under *Whole book*:

- **Narrate the whole book** works through every un-narrated chapter in the background, with
  progress and a stop button. It's hours — 8.4 h for The Institute — so it's meant to run
  overnight. It renders one chapter per turn rather than holding the render lock for the whole
  job, so tapping a single chapter still gets served in between, and stopping lets the chapter
  in flight finish rather than leaving half of one behind. *Narrate part* starts the same run
  scoped to one part, and reports itself that way — it's named in the panel and its progress
  and hours-left are counted over the part, not the book, so four chapters don't show up as
  "3 of 192".
- **Export as audiobook (.m4b)** builds one file from whatever is narrated: chapter markers,
  cover art, title and author, AAC 48 kbps mono. On the phone, tap the download and share it
  into **BookPlayer**, which gives you chapters, sleep timer and position with no PC involved.
  Nothing in the export is player-specific: it's a plain .m4b with ffmetadata chapter marks.
  The full book is ~564 MB; 32 kbps would be ~380 MB but AAC is meaningfully worse than opus
  at that rate, hence 48. *Whatever is narrated* means every part on disk, not only the
  chapters that finished — a chapter cut short still has real audio in it, and the count of
  unfinished and un-narrated chapters is reported alongside the download. Progress and
  failures appear directly under the buttons that start them: on a 192-chapter book the page's
  status line is several screens away from what you'd just tapped.

Storage is `books.json` for the index and `books/<id>/` for the EPUB, extracted text and audio.
At 32 kbps opus a 20-hour book is ~290 MB of parts, plus the export if you make one. All
gitignored, as is `*.epub`.

Every index (`clips.json`, `presets.json`, `chats.json`, `books.json`) is written to a temp
file and renamed. A whole-book render rewrites the book index after every chapter while the
page polls it, and a plain truncate-and-write briefly made the book vanish from the API —
0 failures in 400 reads afterwards.

## How it fits together

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro_worker   (subprocess, English, resident)
                                              ├── piper_worker    (subprocess, Dutch, resident)
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
setup.sh      one-time install of the app venv and all three speech engines
epub.py       EPUB -> chapters of narratable prose
speech.py     Flask app (port 8600). Named speech.py, not app.py, so restart.sh can't
              collide with comfy-webui's (which kills anything matching "app.py").
kokoro_worker.py  resident Kokoro process (English), run with Kokoro's venv interpreter
piper_worker.py   resident Piper process (Dutch), run with Piper's venv interpreter
index.html    the whole UI — one file, no build step
tests/        pytest suite; see Tests below
pytest.ini    test config (rootdir on the import path)
requirements-test.txt   what CI installs — flask, bs4, lxml, pytest, and nothing heavier
.github/workflows/tests.yml   runs the suite on push and pull request
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

**What's covered.** The books state machine, which is where the bugs actually live: a render
being cancelled under itself, what survives a restart, which chapters an export takes, how a
part run scopes its progress, the queue. Then the pure functions — reading a chapter number
out of a heading in digits or words, cutting a chapter into segments and chunks, what gets
announced before the prose — and the HTTP contracts, including the cache headers on narration
audio, which are as much a part of the response as the body.

**What isn't.** Anything you have to hear. Whether a pause is long enough, whether a voice
reads a name right, whether the lock screen looks right — no assertion tells you that, and
writing one would only fix the wrong answer in place. Rendering is stubbed at
`_render_segment`, the one function that costs GPU time; everything above it is real.

`tests/test_export.py` is the exception and runs ffmpeg for real, building actual opus parts
and reading the chapter marks back out of the finished `.m4b` — the only way to know the
markers line up with the audio rather than merely being written. It skips without ffmpeg;
CI asserts ffmpeg is present so a green run can't mean it quietly skipped.

`tests/test_frontend.py` is structural, not behavioural: every `$("#id")` resolves to an
element that exists, no element is left unreferenced, tags balance, ids are unique, and the
inline script parses under `node --check`. One file with no build step means nothing else
catches a dangling reference before the phone does.

**Isolation.** `speech.py` keeps its paths in module globals, so tests point `BOOKS_DIR` and
`BOOKS_FILE` at a tmpdir — autouse, so no test can reach the real library by forgetting to
ask. Don't call `monkeypatch.undo()` in a test: it reverts every patch on the shared
function-scoped `monkeypatch`, that redirect included, and the rest of the test then runs
against your own books. A second autouse fixture snapshots the real library around every test
and fails if it changed, because that mistake is otherwise completely silent.

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
- **Spoken replies leave wavs in `outputs/`**, one per sentence-chunk rather than one per
  reply, so a talkative session accumulates quickly (~190 KB per chunk). They're gitignored
  and safe to delete wholesale.
- **Kokoro has no Dutch voice** — its 54 cover nine languages and Dutch isn't one. That's why
  Piper is here. Kokoro *can* be forced at Dutch (`kokoro-tts -l nl` phonemizes through
  espeak-ng, which knows Dutch) but it's an American accent reading Dutch spelling: on "Het is
  uitstekend gezellig in Scheveningen…" Whisper heard back "in Skeveningen, waar de
  Schoonerhuis in Oudzicht gaven op de Rooige Zee", while Piper's nathalie came back nearly
  intact. Not exposed in the UI; use a Piper voice.
- **Five of Piper's ten Dutch voices are installed.** The rest are lower-quality `x_low`/`low`
  variants of the same speakers, plus `nl_NL-mls-medium`, which is a 52-speaker model needing a
  speaker id the UI doesn't have. Add more by dropping the `.onnx` and `.onnx.json` from
  huggingface.co/rhasspy/piper-voices into `~/.local/share/piper-tts/voices` — they're picked
  up on the next restart, no code change.
