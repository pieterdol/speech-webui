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
- *Piper* — the Dutch engine, because Kokoro has no Dutch voice. Five voices: `alex`, `pim` and
  `ronnie` (nl_NL), `nathalie` and `rdh` (nl_BE), all *medium* quality, ~61 MB each in
  `~/.local/share/piper-tts/voices`. That is as good as Piper's Dutch gets — of the 13 `high`
  quality voices in the catalogue none is Dutch. Faster than Kokoro: a chat reply of 16 s of
  audio rendered in 2 s. Same ▶ preview button, in Dutch. A multi-speaker model, if you install
  one, is offered as one voice per speaker (`nl_NL-mls-medium-2450`), all sharing the one loaded
  model.
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
- **Left out of the narration**, at the foot of the reader and shut by default, lists what
  extraction dropped and why — *Cover · 2w · looks like front or back matter*, *According to the
  National Center for… · 28w · only 28 words*. The Institute loses 21 sections that way, and
  almost all of them should be lost. The list is what the index keeps; a section's words are
  re-read from the stored EPUB only when you ask for them, since `books.json` is rewritten after
  every chapter of every render and twenty sections of prose would be paid for on each write.

  **⤒ on a row** puts those words into *Read this at the start* in ⚙, where you can trim them
  before saving — 28 words of front matter often carry a line you'd rather not hear. Nothing is
  narrated until you save.

  **＋ puts the section back as a chapter of its own instead**, where the book has it. The opening
  note reads one at the top, which is what a dedication wants and no use for an afterword, a
  notice that belongs mid-book, or anything that should have its own marker in the `.m4b`. Once
  in, it's a chapter like any other: narrated, exported, playable, and ⊘ takes it out again if the
  position turns out to be wrong. It lands after the same number of the book's own chapters it
  followed in the spine, and joins the part it lands in front of — a section between two parts
  belongs to the one it introduces rather than splitting the one before it in two. A section
  already back in says so instead of offering ＋ twice.

  Inserting renumbers every chapter after it, and a chapter's number is what the page, the counts
  and your saved position all mean by "chapter", so:

  - **no file is renamed.** Each chapter keeps the number its files are under, so putting a
    section in at position 1 of a 192-chapter book rewrites 192 numbers in `books.json` and moves
    none of the ~400 opus files. Nothing to roll back, and a narrated book stays narrated.
  - **it refuses while that book has anything in the engine or queued**, and says so. A render
    reads which chapter it's about after it takes the lock — which it may have waited an hour for
    — so it would come through the renumbering intact and narrate whatever had moved into the
    position it was handed.
  - **your position moves with the chapters**, and the player on the device doing it is remapped
    rather than stopped. A player on *another* device keeps going and its bookmark is one chapter
    out until that device opens the book again.
  - putting one in **at the top** hands it the title and author, so whatever used to open the book
    has its opening re-recorded — the same move as leaving the front matter out.

  A re-read of the EPUB keeps sections you put back, splicing them in where they were, as long as
  the book's own chapters still line up. When they don't, the confirmation says the sections go
  with everything else — they're one tap each to put back.
- **⊘ leaves a chapter out** of the narration, and **↩** puts it back. For apparatus the rules
  above can't tell from prose: a publisher's list of their own titles is named in the table of
  contents and runs to a few hundred words, which is a chapter as far as any pattern can see.
  A chapter left out is not narrated, not queued by a whole-book run, not counted in what's
  left to do and not in a `.m4b`. It's a mark, not a deletion — the chapter keeps its number,
  its text and any audio it already had, and putting it back costs nothing. A rescan keeps the
  marks, on the same terms as it keeps the audio: only when the chapters still line up.
- **↻ narrates a chapter again** from nothing, on any chapter that has audio. A render keeps every
  part it finds on disk — that's what makes resuming an interrupted chapter cheap — so asking for a
  finished one again changes nothing, which is no use when the audio itself is the problem: a name
  said wrong before there was a respelling for it, an announcement fixed after the fact, a part
  that came out short. It asks first, since a long chapter is hours, and it refuses while that
  chapter is being narrated: the render holds the files it's writing.
- **Narrator** — the same picker as everywhere else, Kokoro English and Piper Dutch. A book
  declaring `nl` gets a Dutch voice by default. Changing it after rendering discards that
  book's audio, so it asks first.
- **🎧 hears how a chapter will start** before anything is committed to it: its announcement and
  the first ~600 characters, which is one call to the engine — about 15 seconds of work for 45
  seconds of audio. Every expensive mistake is in that minute: the wrong narrator, a name the
  voice mangles, a title that reads badly out loud, an opening note with a line in it you'd
  rather not hear. Finding any of them out otherwise costs the eight hours first. It's made
  through the same code a render uses, pauses and all, so it isn't a rehearsal of something else,
  and it's cached under what it actually says — a second tap is instant, and changing the voice,
  the title, the note or a pronunciation makes a new one rather than replaying the old. The
  button is on chapters with nothing to play yet; where there's audio, tapping the row plays it.

  **Tapping it again stops it** — during the render, which abandons it, as well as during the
  minute of audio. The button carries the state: a spinner while the engine is working, **⏹**
  once there's sound. It survives the 4-second refresh, which rebuilds the row it lives on and
  would otherwise strand the only control that could stop it, and it stops itself if the row goes
  altogether — the chapter went into the engine, or you left the book. Starting a voice sample or
  a respelling ▶ ends it too: there's one `<audio>` element behind all three.
- **Rendering** is per chapter, in **~10-minute parts**, and each part appears as soon as it's
  finished — you can start on part 1 while part 2 is still being made. At the measured 2.4×
  realtime, a 5-minute chapter takes ~2 minutes.
- **Playing** — parts are ordinary opus files played by an `<audio>` element, chaining into
  the next part and then the next chapter. Position is saved server-side every 5 seconds, so
  the phone resumes where the PC left off. **⏱** in the bar says the current speed and opens a
  list of the eight worth having, 0.75× to 2× — a 72-pixel range slider is not something you can
  set accurately with a thumb. **↺10 / 30↻** skip
  back ten seconds or forward thirty — asymmetric because going back is for a sentence you
  missed and going forward is for skipping something. A skip crosses into the next part or the
  previous one rather than stopping at the edge of the file that happens to be loaded, using the
  durations in the index to know where a part ends. The lock screen's own skip buttons move by
  the same amounts, so a missed sentence doesn't need the phone unlocked — iOS may still draw
  "10" on its forward button, since the icon is the platform's to choose and only the distance
  is ours.
- **▶ Resume** sits at the top of a book you've started and names where it goes back to — the
  chapter, the part, and how far in. One tap and it plays from there. It's absent while that book
  is the one playing, since the player is the control then, and absent when the position points
  at audio that no longer exists, which is what clearing a narration or changing narrator leaves
  behind. The same **▶** sits on the cover in the library, so a book can be resumed without
  opening it — the player lives outside every view, so nothing needs to be on screen for it to
  play.
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
- **A tap is answered where you tapped it.** Starting, queueing, stopping or leaving a chapter
  out raises a toast at the bottom of the screen — fixed, so it's on screen wherever you are in a
  192-chapter list, and it rides above the player when that's showing. It says which of the two
  things happened, because they're indistinguishable otherwise: *Narrating "X" — roughly 40 min
  of work*, or *Queued "X" — 2 chapters to narrate first*. Renders are serialized across every
  book, so a tap during someone else's chapter changes nothing visible for twenty minutes, and
  the depth it reports is what's actually holding the lock — not the whole-book run's remaining
  chapters, which would say 190 for a chapter that is in fact next. Anything you need to keep
  reading stays in a panel; the toast fades, or a tap dismisses it.
- **Didn't get narrated** appears above *Whole book* when a chapter has failed, names each one
  and what went wrong, and retries the lot with one button. A bulk run steps past an `error`
  chapter rather than trying it again — a chapter whose text has gone would otherwise hold the
  run up all night — so failures accumulate quietly, and the run reports itself finished with the
  book three chapters short. A retry keeps whatever each one managed: four parts on disk are four
  parts the render resumes from. A failed chapter that's also left out isn't retried, since a
  render returns early on one and it would sit in the queue for ever.
- **Narrating now**, above *Whole book*, says which chapter the engine is on and which of its
  parts — "part 1 of 8" from the start, since splitting happens before any audio is made — and
  folds open to what's waiting behind it with a rough total of the work left. It appears only
  when something is happening. The queue is global: renders are serialized *across* books, so
  what's holding this one up can be another book, and the panel names it when it isn't the one
  you're looking at.
- **A queued chapter says so on its own row**, `⏳ queued`, with its Narrate button gone: there's
  nothing left to ask for, and a row that looked untouched made a tap that had worked
  indistinguishable from one that had missed. Only chapters *asked for* are marked — a chapter a
  bulk run will reach on its own keeps its button, because tapping it is how you pull it forward
  past the rest of the run.
- **The book announces itself.** It opens with the title and author — *"Dark Matter" … "by
  Blake Crouch" …* — the way a published audiobook does, and that's also the first thing the
  exported `.m4b` plays. Then before each chapter's prose comes the part's name and the chapter's
  number or title: *"The Night Knocker" … "one" …* and only then the text. The part is spoken only
  where a part actually begins; later chapters in it just get their own heading. What separates the phrases
  is real silence rather than punctuation, a second or so each, and a chapter closes with a
  longer one so it doesn't run into the next announcement. Off via ⚙, which re-renders the book.

  The number is read out of the heading in **digits or in words**, since a book may name its
  chapters "Chapter 1" or "Chapter One", and goes to the engine as digits so that it comes out in
  whatever language the voice speaks — *"nineteen"* from an English one, *"negentien"* from a
  Dutch one. Same reason the *"by"* in front of the author is the book's own word: *"van"* for a
  Dutch book, since the English one read by a Dutch voice comes out as "bie".

  **A heading that is a title is announced as a title**, with the same silence after it — Eragon
  names its chapters *"Palancar Valley"* and never numbers one, and the title read as the first
  line of the prose runs straight into the text with nothing between them. A heading holding both
  is read whole: *"Chapter Seven: Overcoming Obstacles"*, since announcing the seven alone would
  throw the title away. The heading comes out of the text either way, matched without regard to
  case — a book's contents and its pages often disagree about that, and left in, the title is
  narrated twice. What gets no announcement is a section extraction had to name after its own
  first words, because the prose is about to read them out anyway.

  **Read this at the start** in ⚙ is spoken after the author and before the first chapter: a
  dedication or a notice extraction dropped, filled from the *Left out* panel or typed. It's
  chunked like any other text, so a few sentences are a few ordinary calls to the engine rather
  than one long utterance, with a beat between them and a longer pause before the book begins.
  Capped at 1000 characters — it rides the announcement rather than being a chapter, so it has no
  marker of its own in the `.m4b`, and it's no use for something that belongs at the end.

  A title is written to be read, not heard — *11/22/63: A Novel* has a subtitle no narrator says
  out loud — so ⚙ has a **Say the title as** field that only the announcement uses; the library,
  the chapter marks and the `.m4b` keep the written one. The announcement lives in a chapter's
  first part, and what it says is recorded with the chapter as the engine hears it, so renaming
  the book, changing the narrator, adding an opening note or changing how a word is pronounced
  re-makes that one part rather than leaving an opening that no longer matches. What counts as a
  change is asked as *would the opening sound different?* — the same comparison the render makes
  against that record, so the two can't drift, and a book with announcements off answers no.

  The book's own opening belongs to the first chapter it *narrates*, not to chapter 1 — leaving
  the front matter out moves the title and author onto whatever comes first now, and the chapter
  gaining or losing them has its first part re-made on the spot.
- **Say these words differently** is the book's own pronunciation list, in ⚙: written form on the
  left, how it should sound on the right, ▶ to hear it in this book's voice before committing to
  it. The names in one novel are nobody else's problem, so this sits on the book, on top of the
  global `RESPELL`. Whole words, any capitalisation; an empty spoken form means don't say the word
  at all.

  **How does the book spell it?** searches the chapter text and answers with the spellings that
  are actually printed, commonest first, with counts and a phrase to see one in — tap a spelling
  and it fills the written field. A respelling is keyed on the written form, which is the one
  thing a narrator saying it wrongly can't tell you, and it's worth knowing what you're up
  against: Dark Matter prints *Jason*, *Jason2*, *Jasons*, *Jason4* and *Jason9*, so respelling
  the first fixes a fifth of them. Forms are runs of word characters, so a search answers
  *Vermeer* rather than *Vermeer's* — that's what a rule is keyed on, and it matches the
  possessive anyway. Under two letters isn't a search. About 50 ms over a 23-chapter book and
  270 ms over a 192-chapter one.

  Editing the list changes nothing by itself: **one save applies the lot**, so fixing three names
  is one round of re-narrating rather than three, and the button says how many changes are
  waiting. Leaving the book forgets an unsaved draft, which is two taps to redo and cheaper than
  a stale copy of your intentions on disk.

  Saving re-narrates **only the parts that said it the old way** — usually a single ten-minute
  part per occurrence out of a book of hundreds, which is what makes fixing a name on chapter
  forty affordable. Which parts, exactly, is decided by asking whether the text the engine would
  be handed changes, not by searching for the word: that catches a removed entry (the audio still
  says the respelled form), an entry that fires on another rule's output, and one keyed `Doctor`
  reaching text that reads `Dr. Who`. A word in the title, the author, a part name or a chapter's
  own heading is caught through the recorded opening instead — that's where all four are spoken,
  the heading having been dropped from the text before it's read.

  Everything the change invalidates is deleted at once, so no export can pick it up, and every
  affected chapter is queued — a finished book stays finished. Past a couple of chapters it says
  what it will cost first: one common word would correctly re-narrate everything. A run in flight
  is not interrupted; if a save lands mid-chapter, that chapter is left pending rather than marked
  ready, and the next pass fills the gap.
- **The EPUB itself** can be taken off the machine, from under the respellings in ⚙, which is
  where you'd want it: a name is easier to copy off the page it's printed on than off a narrator
  saying it wrongly. It downloads, shares or opens in Safari the same three ways an export does,
  and arrives called *Dark Matter.epub* rather than `book.epub` — one resolver behind
  `/export/<book>/<name>` answers for both, so the file and the page wrapped around it for iOS
  can't disagree about what exists. The EPUB is reachable **only** under the book's own name;
  asking for `book.epub`, or for another book's, is a 404, and it can't be deleted through the
  export endpoint.
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
  **Replace the cover** upload, for books that declare none and for when the publisher's is
  not the one you want. The files are cached for a day and keep their names, so every place
  that shows a cover asks for it with `?v=`, a version taken from the image on disk — a
  replacement appears in the grid, the header, the player and the lock screen at once.

**Listening away from this PC.** Two buttons under *Whole book*:

- **Narrate the whole book** works through every un-narrated chapter in the background, with
  progress and a stop button. It's hours — 8.4 h for The Institute — so it's meant to run
  overnight. Tapping a single chapter still gets served in between, and stopping lets the
  chapter in flight finish rather than leaving half of one behind. *Narrate part* is the same
  run scoped to one part, counting its progress and hours-left over the part rather than the
  book.

  **Anything long enough says the time it finishes**, not only how long it takes — *roughly
  8.4 h of work — done by about 06:40 tomorrow*. On a phone at bedtime that's the number you
  can act on, and the day is named when the answer isn't today's, since a bare *06:40* would
  read as this morning. Under half an hour it's the duration alone. The clock is the browser's,
  so the phone answers in its own time.
- **A run can be added to while it runs.** There's one run per book, and it covers a set of
  parts — asking for another part while one is being narrated queues that part behind it, and
  asking for the whole book widens the run to everything. Nothing is interrupted and nothing
  starts twice: the worker re-reads what it covers between chapters, so a single worker per book
  picks up whatever has been added. The panel counts over the run's whole scope, and its buttons
  say which state they're in — a part being narrated shows a spinner and can't be pressed, and
  *Narrate the whole book* is only dead once the run already covers it.

  The scope lives in `render_all.parts`, `[]` meaning the whole book. A run started before that
  existed carries a single `part` name instead, which is read the same way rather than migrated.
- **Export the audiobook when a run finishes**, a per-book toggle in ⚙, does the last two taps
  for you: a whole-book run is hours and gets started at bedtime, so the `.m4b` is waiting in the
  morning rather than needing the phone picked up first. Off by default — an export is a few
  hundred megabytes and its own hour of ffmpeg. Only a run that worked through everything it
  covers exports; stopping one is the answer to "not like this", and half a book isn't what you
  asked for. A run over a single part exports that part, and a wider one exports the whole book.
  The encode runs in the finished run's own thread, so it can't race the narration, and since
  nobody is polling a job at four in the morning the outcome goes to `speech.log` as well.
- **Export as audiobook (.m4b)** builds one file from whatever is narrated: chapter markers,
  cover art, title and author, AAC 48 kbps mono. On the phone, tap the download and share it
  into **BookPlayer**, which gives you chapters, sleep timer and position with no PC involved —
  nothing in the export is player-specific. *Whatever is narrated* means every part on disk, not
  only the chapters that finished, and the count of unfinished and un-narrated chapters is
  reported alongside the download. The full book is ~564 MB. On the phone the same file is
  offered through the iOS share sheet instead of as a download — see the constraint below.
- **Exported audiobooks** is the one place an export appears, the one just built included. Every
  `.m4b` the book still has on disk, newest first, each with what went into it — chapters, how
  many were unfinished or not narrated, how long it plays — its size, when it was built, and the
  buttons to take it or delete it. Taking a second copy never needs the book re-encoded. A book
  that has never been exported shows no list at all, and while one is being built the panel above
  says so and nothing else.

  What an export came to used to live only in the job's result, so it went with the next reload
  while the file it described stayed. It's written beside the file now, as `<name>.m4b.json`; an
  export built before that just says its size and date. One built before a pronunciation changed
  says **⚠ says a word the old way** — it isn't deleted, since rebuilding is hours of ffmpeg and
  the copy already on a phone is fine, but it shouldn't be shared again unnoticed.
- **An export being encoded is never offered.** ffmpeg writes to `<name>.m4b.part` and the file
  is renamed when it's whole — the listing only knows `.m4b`, so a half-built audiobook can't be
  shared or deleted, and a killed encode leaves a `.part` (swept on the next start) rather than a
  truncated `.m4b` that looks finished. The rename is what the extension used to do for ffmpeg,
  so the muxer is now named outright: `-f ipod`, byte-for-byte what `.m4b` selected before.
- **Deleting an export** takes that one file and nothing else: the narration stays, so the book
  can be exported again without narrating a word. These are the largest files the app makes —
  137 MB for one book, and the three exported here are 199 MB together — and the only other way
  to remove one is *Clear narration*, which also deletes every chapter's audio. The filename
  arrives over the wire, so it goes through `safe_path` and has to end in `.m4b`: nothing else in
  the export directory, and nothing outside it, can be deleted through that endpoint.

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

**A chapter's number and its filename are two different things.** `i` is the position in
`book["chapters"]` — what everything means by a chapter, including the saved position — and files
are named after the number the chapter was *created* with, kept in `key` when the two differ. They
only differ in a book that has had a section put back into it; anywhere else `key` is absent and
the two are the same number, which is why nothing else in the app had to change. Every path goes
through `text_file`, `audio_file` and `audio_name` so there's one place to check that.

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

**Kokoro reads 510 phonemes at a time**, and the worker does the splitting rather than leaving it
to `kokoro-onnx`, whose own splitter only breaks at punctuation: text without any — a page of
book titles, one per line — comes back as one oversized batch, and the model then indexes the
voice's style table by the token count and falls off the end of it. So the text is phonemized
once, cut at the latest punctuation, space or character that fits under the limit, and the
pieces are read in turn and joined. It has to be counted in phonemes, not characters: English
runs about 1.1 phonemes per character and *"$100,000"* is thirty, so no limit on the text could
stand in for it.

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

**What's covered.** Every module, ~630 tests. The books state machine, which is where the bugs
actually live: a render cancelled under itself, what survives a restart, which chapters an export
takes, how a part run scopes its progress, the queue, what leaving a chapter out takes it out of,
and which audio a changed pronunciation invalidates — including a save landing mid-render. What a
section put back as a chapter renumbers and what it deliberately doesn't move, since a position and
a filename mean different things only there.
The pure functions — reading a chapter number out of a heading, cutting text into segments,
chunks, sentences and phoneme batches, what gets announced before the prose. The stores behind
clips, presets and chats. And the HTTP contracts, including the cache headers on narration audio.

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
left unreferenced, tags balance, ids are unique, the inline script parses, and no download link is
built outside the one helper that knows what iOS does with them. One file with no
build step means nothing else catches a dangling reference before the phone does. The parse check
uses `node --check` where node exists, which is what CI runs, and falls back to esprima where it
doesn't — a desktop is not a build box, and the check is worth most on the machine the page is
edited on. A second test feeds both of them a script that doesn't parse, since a checker that
passes everything is worse than none.

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
- **A download can't finish inside the home-screen app** — on the phone the audiobook is
  *shared*, not downloaded. Two separate iOS behaviours make the download impossible, and the
  page detects the app with `navigator.standalone`:

  A plain link puts the file on a full-screen "Open in…" splash with the app hidden behind it,
  and a home-screen app has no toolbar to leave it with, so the app has to be force-quit (WebKit
  236943). Adding `target="_blank"` escapes that — the link opens in a browser view that does
  have a Done button — but that view is handed a file rather than a page, so it renders blank
  and greys out both its Share and its Open-in-Safari buttons. Nothing on that screen can save
  the file.

  So **Share the .m4b** fetches the file in the page and hands it to `navigator.share`, and the
  share sheet passes it to BookPlayer directly — the Files detour a download would need doesn't
  happen. It takes two taps on a large book: iOS opens the sheet only during a fresh tap and a
  hundred megabytes over the tailnet outlasts that, so the first tap fetches (showing progress)
  and the second one shares, the file kept in hand between them. **Open in Safari** is the
  fallback, and points at `/get/…` rather than the file: a page is something that browser view
  can render, which leaves *its* Open-in-Safari button live, and a download in real Safari
  behaves normally and lands in Files.

  Every other browser keeps the plain `download` link, which is why this is invisible on the PC.
- **Whether a download finished is in `speech.log`.** The access log can't tell you: an
  abandoned transfer and a completed one are both a `200` with no size, and iOS discards a file
  it can't hand anywhere. So the export route counts the bytes it actually streams —
  `sent 16384 of 137022062 bytes — INCOMPLETE · range=- · ua=…` — which separates a phone that
  never asked from one that read the whole file and threw it away. The User-Agent is in there
  because every request arrives from `127.0.0.1` through the tailnet proxy, so nothing else says
  which device it was.
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
- **Fixing a mispronunciation means respelling it**, at one of two levels. `RESPELL` in
  `textprep.py` is the global map, for words any book gets wrong — `movies` → `movees`, because
  espeak clips the `-ies` to "movis". A book also carries its own, `book["respell"]`, edited from
  ⚙ and applied on top; where both name a word the book wins. Kokoro's documented
  `[word](/phonemes/)` override belongs to the KPipeline package and not to `kokoro_onnx`, which
  reads the markup itself out loud, so respelling is the whole toolkit.

  A replacement goes through `re.sub` as a *callable*, never as a template. It's typed in by
  hand, and as a template `AC\DC` or `\1` would be read as a backreference and raise inside a
  render thread.
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
- **Five of Piper's ten Dutch models are installed.** The rest are `x_low`/`low` variants of
  speakers already here, plus `nl_NL-mls-medium`, which is left out on purpose: its 52 readers
  come from LibriVox recordings and each one has too little training data to say a short sentence
  — under about ten words they produce unrelated audio, so "Hij knikte." lands as "41, 41" in the
  middle of otherwise fine prose. Fiction is full of short sentences, so it can't narrate.
  Add other voices by dropping the `.onnx` and `.onnx.json` from
  huggingface.co/rhasspy/piper-voices into `~/.local/share/piper-tts/voices` — picked up on the
  next restart, no code change, and a multi-speaker model is expanded to one voice per speaker.

## License

MIT, see [LICENSE](LICENSE). The speech engines it drives are separately licensed, and the books
in `books/` are bought copies that stay out of the repository.
