# Books: EPUB narration

The rules behind the feature list in the [README](../README.md) — the ones you can't discover by
looking at the screen.

## Extraction

`epub.py` reads the spine for reading order and the `.ncx` for chapter names, then drops covers,
colophons, adverts, dedications, lists of illustrations and part-title pages. A chapter the table of
contents names is kept however short it is; the length rule that drops front matter applies to
untitled sections only.

A Project Gutenberg text is dropped at both ends: its header page and its 343-word licence are long
enough to be chapters and neither one is the book. A book *about* Gutenberg loses its chapters to
that, and the *Left out* panel is where you'd see it and put them back.

A book that packs several chapters into one file and addresses each from its contents by anchor —
`Section0001.xhtml#heading_id_3` — is cut at those anchors, so twenty chapters stay twenty chapters.

Three folding rules keep one chapter from becoming two:

- **A title set on a page of its own belongs to the chapter after it.** Where a book gives a chapter
  a page carrying only its title and then the chapter itself on an unnamed page, the two are folded
  into one under the name the contents gives, and the title page becomes the chapter's heading. A
  short section that *isn't* the next chapter's title, like a stray quotation, stays a chapter of its
  own: the test is that its whole text is inside its name.
- **A part's title page belongs to the chapter under it, as a part.** Where the line under the part
  opens with a number, that line is the chapter, and the name becomes `BOOK ONE … · I. THE EVE OF
  THE WAR.` — announced as the part and then chapter one.
- **What the announcement is about to say never survives in the prose.** A half-title page printing
  the book's title and author over the first chapter has both taken off the top, in whichever order
  the page sets them, by the same rule that removes a heading. A bare number is left alone: by then
  it's the book's own numbering inside the chapter.

## What the book is about

Adding a book looks it up on Open Library — a title and an author out, a paragraph back, cut to
two or three sentences and shown under the title on the book's own page. It's never narrated.

**`OPENLIBRARY=0` turns the automatic lookup off**, and then nothing leaves this machine on its
own. **↻** in ⚙ still asks, because tapping it is the request; a button that did nothing would be
worse than no button. Read at startup like the other settings, so it takes a restart.

This is the only part of the app that talks to another machine, so it's built to be ignorable:
the lookup runs off the upload rather than inside it, an import neither waits on it nor fails
with it, and a book that turns up nothing simply hasn't got one. The match is a guess made from
a title and an author, and the catalogue will happily return a different book of the same name —
so the work it matched is recorded beside the description, **↻** in ⚙ asks again, and the field
takes whatever you write over it, including nothing.

## Left out of the narration

The panel lists what extraction dropped and why. It's what the index keeps; a section's words are
re-read from the stored EPUB only when you ask for them, since `books.json` is rewritten after every
chapter of every render.

**⤒** puts those words into *Read this at the start*, where you can trim them before saving — a few
words of front matter often carry a line you'd rather not hear.

**＋ puts the section back as a chapter of its own**, where the book has it, which is what an
afterword, a mid-book notice or anything wanting its own `.m4b` marker needs. It lands after the same
number of the book's own chapters it followed in the spine, and joins the part it lands in front of: a
section between two parts belongs to the one it introduces rather than splitting the one before it.

Inserting renumbers every chapter after it, and a chapter's number is what the page, the counts and
your saved position all mean by "chapter", so:

- **No file is renamed.** Each chapter keeps the number its files are under, so an insert rewrites
  every number in `books.json` and moves no audio. Nothing to roll back, and a narrated book stays
  narrated.
- **It refuses while that book has anything in the engine or queued.** A render reads which chapter
  it's about after it takes the lock, so it would come through the renumbering intact and narrate
  whatever had moved into the position it was handed.
- **Your position moves with the chapters**, and the player on the device doing it is remapped rather
  than stopped. A player on *another* device keeps going and its bookmark is one chapter out until
  that device opens the book again.
- **An insert at the top** hands that section the title and author, so whatever opened the book
  before has its opening re-recorded.

A re-read of the EPUB keeps sections you put back, splicing them in where they were, as long as the
book's own chapters still line up. When they don't, the confirmation says the sections go with
everything else — one tap each to put back.

## Leaving a chapter out, and doing one again

**⊘** is for apparatus the extraction rules can't tell from prose: a publisher's list of their own
titles is named in the contents and runs to a few hundred words, which is a chapter as far as any
pattern can see. A chapter left out is not narrated, not queued by a whole-book run, not counted in
what's left to do and not in a `.m4b`. It's a mark, not a deletion — the chapter keeps its number, its
text and any audio it had. A rescan keeps the marks on the same terms as the audio: only when the
chapters still line up.

**↻** narrates a chapter again from nothing. A render otherwise keeps every part it finds on disk,
which is what makes resuming cheap but means asking for a finished chapter again changes nothing. It
asks first, and it refuses while that chapter is being narrated: the render holds the files it's
writing.

**🎧** renders the announcement and the first ~600 characters, one call to the engine. Every expensive
mistake is in that minute: the wrong narrator, a name the voice mangles, a title that reads badly out
loud, an opening note with a line in it you'd rather not hear. It's made through the same code a
render uses, pauses and all, and cached under what it actually says, so a second tap is instant and
changing the voice, the title, the note or a pronunciation makes a new one. Tapping it again stops it,
during the render as well as during the audio; starting a voice sample or a respelling ▶ ends it too,
since there's one `<audio>` element behind all three.

## Rendering, queueing and playing

**Rendering** is per chapter, in **~10-minute parts**, each appearing as soon as it's finished. At the
measured 2.4× realtime, a 5-minute chapter takes about 2 minutes. A part or whole-book run can be
stopped, a single chapter can't, so anything over ten minutes of work asks first.

**The queue is global**: renders are serialized *across* books, so what's holding this one up can be
another book, and the *Narrating now* panel names it when it isn't the one you're looking at. A tap
therefore has to say which of two things happened — *Narrating "X" — roughly 40 min of work* or
*Queued "X" — 2 chapters to narrate first* — and the depth it reports is what is actually queued, not
the whole-book run's remaining chapters. Only chapters *asked for* are marked `⏳ queued`; a chapter a
bulk run will reach on its own keeps its Narrate button, because tapping it is how you pull it forward
past the rest of the run.

**One list, in order, and one worker draining it.** A queued chapter used to be a thread blocked on a
lock, which meant the order was whatever Python handed out, a hundred queued chapters were a hundred
blocked threads, and nothing could be taken off — a thread waiting on a lock can't be interrupted,
which is the point of a lock. Now the queue is a list: it says what happens next, in the order it was
asked for, and **✕ takes a chapter off it**. The worker ends when the list empties and the next thing
queued starts another, so "one book renders at a time" is the shape of the thing rather than a promise
a lock is asked to keep. A chapter that throws is recorded on its own row and the queue carries on.

**The chapter being narrated can't be taken off** — stopping it part-way through a ten-minute part
would leave a file nothing finishes, which is why a single chapter has never had a stop button. A
whole-book run queues one chapter at a time and waits for it, so a chapter you tap during a run is
next rather than 190th; ✕ on the chapter a run is waiting for makes the run step over it rather than
ask again, and ⊘ is how you leave one out for good.

**Failures accumulate quietly.** A bulk run steps past an `error` chapter rather than trying it again
— a chapter whose text has gone would otherwise hold the run up all night — so a run can report itself
finished with the book a few chapters short. *Didn't get narrated* names each one and what went wrong,
and retries the lot with one button, keeping whatever each managed: four parts on disk are four parts
the render resumes from. A failed chapter that's also left out isn't retried, since a render returns
early on one and it would sit in the queue for ever.

**Playing.** Parts are ordinary opus files played by an `<audio>` element, chaining into the next part
and then the next chapter. Position is saved server-side every 5 seconds. **↺10 / 30↻** are asymmetric
because going back is for a sentence you missed and going forward is for skipping something; a skip
crosses into the next part or the previous one rather than stopping at the edge of the loaded file,
using the durations in the index to know where a part ends. The lock screen's own skip buttons move by
the same amounts; iOS may still draw "10" on its forward button, since the icon is the platform's to
choose and only the distance is ours.

The player lives outside every view, so nothing needs to be on screen for it to play, and the book it
belongs to is not necessarily the one you're looking at. Only the book actually playing is stopped by
a re-narration. Reaching the end of what exists re-reads the book once before giving up — running out
is exactly when a render may have finished the next part. **▶ Resume** is absent while that book is
the one playing, and absent when the position points at audio that isn't there any more.

## Runs

**Narrate the whole book** works through every un-narrated chapter in the background. Tapping a single
chapter still gets served in between, and stopping lets the chapter in flight finish rather than
leaving half of one behind. *Narrate part* is the same run scoped to one part, counting its progress
and hours-left over the part rather than the book.

**Anything long enough says the time it finishes**, not only how long it takes — *roughly 6.2 h of
work — done by about 04:20 tomorrow*. The day is named when the answer isn't today's, since a bare
*04:20* would read as this morning. Under half an hour it's the duration alone. The clock is the
browser's, so the phone answers in its own time.

**A run can be added to while it runs.** There's one run per book, covering a set of parts: asking for
another part while one is being narrated queues that part behind it, and asking for the whole book
widens the run to everything. Nothing is interrupted and nothing starts twice — the worker re-reads
what it covers between chapters. The scope lives in `render_all.parts`, `[]` meaning the whole book.

## Announcements

**The book announces itself** with its title and author — *"Some Title" … "by A Writer" …* — the way a
published audiobook does, and that's also the first thing the exported `.m4b` plays. Then before each
chapter's prose comes the part's name and the chapter's number or title. The part is spoken only where
a part actually begins. What separates the phrases is real silence rather than punctuation, a second
or so each, and a chapter closes with a longer one so it doesn't run into the next announcement. Off
via ⚙, which re-renders the book.

The number is read out of the heading in **digits, in words or in roman numerals** — a book may write
"Chapter 1", "Chapter One" or "Chapter I" — and goes to the engine as digits so that it comes out in
whatever language the voice speaks: *"nineteen"* from an English one, *"negentien"* from a Dutch one.
Same reason the *"by"* in front of the author is the book's own word: *"van"* for a Dutch book, since
the English word read by a Dutch voice comes out as "bie".

A numeral is only a number where a heading says it is — behind "chapter" or "part", or opening the
heading with a stop after it, *"II. The Falling Star"*. A bare `I` anywhere else is the pronoun. The
strict form only, and capped at 999, which between them throw out the English words spelled in numeral
letters: `MIX` is a valid 1009.

**A heading that is a title is announced as a title**, with the same silence after it, and a heading
holding both is read whole — *"Chapter Seven: Overcoming Obstacles"* — since announcing the seven alone
would throw the title away. The heading comes out of the text either way, matched without regard to
case, since a book's contents and its pages often disagree about that and left in, the title is
narrated twice. What gets no announcement is a section extraction had to name after its own first
words, because the prose is about to read them out anyway.

**The book's title is said once.** A page carrying nothing but the title becomes a chapter, or the
part above the first one, and the announcement would then say the same thing twice over — *"The Time
Machine" … "by H G Wells" … "The Time Machine" … "1. Introduction"*. A part or heading the title has
just said is dropped, the start of the title included, since such a page is often cut short of the
subtitle. Four characters at least, so nothing is dropped for sharing a word with the title.

**Read this at the start** is spoken after the author and before the first chapter. It's chunked like
any other text, so a few sentences are a few ordinary calls to the engine with a beat between them and
a longer pause before the book begins. Capped at 1000 characters — it rides the announcement rather
than being a chapter, so it has no marker of its own in the `.m4b`, and it's no use for something that
belongs at the end.

**Say the title as** is for a title written to be read rather than heard: *11/22/63: A Novel* has a
subtitle no narrator says out loud. Only the announcement uses it; the library, the chapter marks and
the `.m4b` keep the written one.

The announcement lives in a chapter's first part, and what it says is recorded with the chapter as the
engine hears it, so renaming the book, changing the narrator, adding an opening note or changing how a
word is pronounced re-makes that one part rather than leaving an opening that doesn't match. What
counts as a change is asked as *would the opening sound different?* — the same comparison the render
makes against that record, so the two can't drift, and a book with announcements off answers no.

The book's own opening belongs to the first chapter it *narrates*, not to chapter 1 — leaving the front
matter out moves the title and author onto whatever comes first now, and the chapter gaining or losing
them has its first part re-made on the spot.

## Pronunciation

**Say these words differently** sits on the book, on top of the global `RESPELL` — see
[gotchas.md](gotchas.md) for the two levels. Whole words, any capitalisation; an empty spoken form
means don't say the word at all.

**How does the book spell it?** answers with the spellings actually printed, commonest first, with
counts and a phrase to see one in. A respelling is keyed on the written form, which is the one thing a
narrator saying it wrongly can't tell you, and a name is often printed several ways. Forms are runs of
word characters, so a search answers *Vermeer* rather than *Vermeer's* — that's what a rule is keyed
on, and it matches the possessive anyway. Under two letters isn't a search.

Editing the list changes nothing by itself: **one save applies the lot**, so fixing three names is one
round of re-narrating rather than three. Leaving the book forgets an unsaved draft.

Saving re-narrates **only the parts that said it the old way** — usually a single ten-minute part per
occurrence out of a book of hundreds, which is what makes fixing a name on chapter forty affordable.
Which parts, exactly, is decided by asking whether the text the engine would be handed changes, not by
searching for the word: that catches a removed entry, an entry that fires on another rule's output, and
one keyed `Doctor` reaching text that reads `Dr. Who`. A word in the title, the author, a part name or
a chapter's own heading is caught through the recorded opening instead — that's where all four are
spoken, the heading having been dropped from the text before it's read.

Everything the change invalidates is deleted at once, so no export can pick it up, and every affected
chapter is queued — a finished book stays finished. Past a couple of chapters it says what it will cost
first: one common word would correctly re-narrate everything. A run in flight is not interrupted; if a
save lands mid-chapter, that chapter is left pending rather than marked ready, and the next pass fills
the gap.

## More than one voice

**Who speaks here** works out, chapter by chapter, who says each quoted line, and gives each character
a voice of their own. The narration between the quotes stays with the book's narrator. It's opt-in per
chapter and costs about **a minute** of GPU for a chapter of 130 quoted lines.

**Thinking is off**, and that's the difference between a minute and not working at all. `qwen3`
reasons out loud unless told otherwise, and on this it spends the reasoning instead of the answer:
one window of a hundred runs went 33,000 tokens into a think block and never reached the JSON —
still emitting at 40 tokens a second after the client had hung up — and with the answer capped it
reached the cap *empty*. Told not to think, the same chapter came back in 49 seconds with every run
answered. Attribution is a judgement per marker, not a problem to work through.

**Narrating it afterwards costs nothing extra**, which is worth saying because it looks like it should:
a voice change is a separate call to the engine, and a dialogue-heavy chapter is 500 calls where one
voice is 80. Measured on the same chapter, seven voices came out at 2.6× realtime against 2.4× read in
one, and 42.2 minutes of audio against 42.6. Kokoro's fixed cost is a second or two per call and a run
is long enough that generation dominates it.

The work is split between code and the model on purpose. **Code finds the quoted runs**; the model is
only ever asked *who says number 14* about runs already marked in the text. Asked instead to quote a
character's first line back, a local model gets it wrong about half the time — it answers with
narration. Two more things code settles rather than asking:

- **A named speaker wins.** Where the chapter says "said Bingley" or "Daniela says" right after a run,
  that's the answer, whatever the model thought. Only names the model already found count, or "said
  the man in the mask" would cast a character called *The*.
- **Gender from the pronoun.** "he says" makes a speaker male even where the model answered *unknown* —
  it reads the question as "has this person been identified", and ninety lines of a man in a mask
  otherwise go to the narrator for want of a voice.

**A speech split by its tag is one voice.** `"…," said Marla, "…"` is two runs and one person; two runs
with a full stop between them can be two people, and each is answered on its own.

**One person written two ways.** A chapter names a character part-way through, so the runs before that
come back as `the man` and the ones after as `Leighton Vance`: two entries in the cast, two voices, one
person changing voice halfway. It can't be settled while reading — at run 4 the chapter genuinely
hasn't said — so there's **one more question at the end**, asked about the cast rather than the
chapter: each speaker with a few of their lines, which of these are the same person. First, longest and
last, because the line that settles it is the one where he says *"I'm Leighton Vance, chief
executive"*, and that is never anyone's first line.

Code keeps only the safe answers: a name that isn't in the list is refused, a merge across genders is
refused, a chain is followed to its end and a loop is dropped whole. A wrong merge is two characters
read in one voice, which is the failure the pass exists to avoid.

**It catches some and not all**, measured on one chapter: `a woman` → Amanda Lucas and `a man` → `the
man`, while `the man` stayed beside Leighton Vance and `the narrator` beside Jason Dessen. Two things
that look like they'd fix that don't. Telling the model in the prompt to use the name for the earlier
lines as well made it stop using names at all — three quarters of a chapter came back as `the man`. And
letting the consolidation pass *think* made it merge nothing and take three times as long, the
reasoning having spent the token cap before it wrote an answer. So the last word is yours: **two
characters may share a voice** when you set one by hand, which is how `the man` and Leighton Vance
become one person. Nothing assigns a voice twice on its own.

**The answer is capped**, at about four times what a window needs. Asked for a JSON array a model
can decide never to stop writing one, and one did: 33,000 tokens into a chapter of 130 quoted lines,
still emitting entries at 40 a second with nothing to stop it but the request timeout, and still going
after the client had hung up. A cap makes that a truncated answer instead of a hung chapter, and
truncated is already handled — the runs it didn't reach are `unknown`, which the narrator reads.

**Model.** `qwen3:14b`, and the size earns its place: on a chapter of Austen 8b split a speech between
two speakers, read an illustration caption as dialogue and wrote one speaker's name two ways, where 14b
got all twenty-one runs right. It costs no more wall-clock, spending fewer tokens to get there. A long
chapter is asked about a **window at a time** — 24,000 characters, cut on line boundaries so no run is
split — and each window is told who spoke the last few runs of the one before it.

**Casting.** A character gets a voice of the narrator's own accent and their own gender, never the
narrator's and never one already taken by somebody else, with the most-spoken cast first so it's the passers-by who go
without when the voices run out. The map lives on the book, so somebody who speaks in four chapters
sounds the same in all four, and attributing another chapter adds to it without re-casting anyone
you've already heard. A speaker whose gender the chapter never shows keeps the narrator's voice:
guessing is wrong half the time, and a man read in a woman's voice is worse than the narrator reading
his line. **Dutch stays single-voiced** — Piper has one or two voices installed at all, and two
characters sharing a voice is worse than one narrator reading both.

**Every line is listed** with who says it and whether code or the model decided, because reading them
is the only way to see an attribution is wrong without listening to an hour of narration. Changing a
character's voice re-narrates the chapters they speak in and no others.

**The cast list says where you meet them** — *first in Chapter Two · part 1 · 32 lines* — because a
name on its own is no help deciding what somebody should sound like, and a part is a thing you can go
and press play on. **💬** opens their lines under their row, each with the part it's in: that's how you
tell *the man* is Leighton before he gives his name, without listening for it. Forty lines at a time,
and it says how many it left. Everyone who speaks is listed, including the ones the narrator reads —
the voice map holds only the names that were given a voice, and "who is this" is a question about all
of them.

**A book with a cast keeps it.** Once one chapter has been worked out, any chapter narrated after it
is worked out first — the render asks for that itself rather than reading it in one voice. That
matters because narration is mostly not asked for by hand: playing a chapter asks for the next one, to
stay ahead of the listener, and without this reaching chapter two of a seven-voice book quietly went
back to one. It costs a couple of minutes on top of a chapter that takes twenty, it happens inside the
render lock so two renders can't ask about the same chapter at once, and a model that isn't there is
not a failure — the chapter is narrated in one voice, which is what it would have been anyway. A book
with **no** cast is left alone: nobody asked it for voices, and minutes of GPU per chapter is not
something to start on its own.

What this doesn't do is go back over chapters **already narrated** in one voice. Their audio is real
and nothing throws that away without being asked; ↻ on the row is how you redo one, and the row saying
`🎭 7 voices` is how you tell which is which.

The attribution is one list per chapter in `books/<id>/cast/chNNN.json`, keyed by run number; the
voices are `cast` on the book. Rendering **checks the count of runs before it trusts it**: a chapter
re-scanned since would otherwise shift every voice after the change by one, which sounds like a broken
cast rather than a stale file, so a mismatch reads the chapter in one voice. A voice moved under a
chapter mid-render leaves it pending rather than ready, the same way a pronunciation change does — and
a character's lines are anywhere in a chapter, so that takes all of it rather than one part.

## Export

**Export as audiobook (.m4b)** builds one file from whatever is narrated: chapter markers, cover art,
title and author, AAC 48 kbps mono, muxed with `-f ipod`. *Whatever is narrated* means every part on
disk, not only the chapters that finished, and the count of unfinished and un-narrated chapters is
reported alongside the download. On the phone the file is offered through the iOS share sheet instead
of as a download — see [gotchas.md](gotchas.md).

Every `.m4b` the book still has on disk is listed with what went into it, which is written beside the
file as `<name>.m4b.json`. One built before a pronunciation changed says **⚠ says a word the old way**
— it isn't deleted, since rebuilding is hours of ffmpeg and the copy already on a phone is fine, but it
shouldn't be shared again unnoticed. Taking a second copy never needs the book re-encoded.

**An export being encoded is never offered.** ffmpeg writes to `<name>.m4b.part` and the file is
renamed when it's whole, and the listing only knows `.m4b`, so a half-built audiobook can't be shared
or deleted and a killed encode leaves a `.part` (swept on the next start) rather than a truncated
`.m4b` that looks finished.

**Deleting an export** takes that one file and nothing else: the narration stays, so the book can be
exported again without narrating a word. The filename arrives over the wire, so it goes through
`safe_path` and has to end in `.m4b`.

**Export the audiobook when a run finishes**, a per-book toggle, does the last two taps for you: a
whole-book run gets started at bedtime, so the `.m4b` is waiting in the morning. Off by default — an
export is a few hundred megabytes and its own hour of ffmpeg. Only a run that worked through everything
it covers exports; stopping one is the answer to "not like this". A run over a single part exports that
part, a wider one the whole book. The encode runs in the finished run's own thread, so it can't race
the narration, and the outcome goes to `speech.log` as well, since nobody is polling a job at four in
the morning.

**The EPUB itself** can be taken off the machine, from under the respellings in ⚙: a name is easier to
copy off the page it's printed on than off a narrator saying it wrongly. It downloads, shares or opens
in Safari the same three ways an export does, and arrives under the book's title rather than as
`book.epub` — one resolver behind `/export/<book>/<name>` answers for both, so the file and the page
wrapped around it for iOS can't disagree about what exists. The EPUB is reachable **only** under the
book's own name; asking for `book.epub`, or for another book's, is a 404, and it can't be deleted
through the export endpoint.

## Covers and storage

Two cover sizes are derived once with ffmpeg, since the original is often a couple of megabytes and
not worth sending a phone repeatedly: **thumb** for everywhere it appears small and **full** for the
lock screen and the `.m4b`'s artwork. Both cap rather than resize, so a smaller cover is left alone
rather than blown up, and both keep the book's proportions.

The cover is whichever image the book *declares* — EPUB 3 `properties="cover-image"`, then the EPUB 2
`<meta name="cover">` id, then the guide reference — never the first or biggest image, since a book's
back matter can carry the covers of other novels advertised in it. The files are cached for a day and
keep their names, so every place that shows a cover asks for it with `?v=`, a version taken from the
image on disk — a replacement appears in the grid, the header, the player and the lock screen at once.

`books.json` holds the index; `books/<id>/` holds the EPUB, the extracted text and the audio. At 32
kbps opus a 20-hour book is ~290 MB of parts, plus the export if you make one. All gitignored, as is
`*.epub`.
