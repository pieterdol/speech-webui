# Books: EPUB narration

The rules behind the feature list in the [README](../README.md) — the ones you can't discover by
looking at the screen.

## Extraction

`epub.py` reads the spine for reading order and the `.ncx` for chapter names, then drops covers,
colophons, adverts, dedications and part-title pages. A chapter the table of contents names is kept
however short it is; the length rule that drops front matter applies to untitled sections only.

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
*Queued "X" — 2 chapters to narrate first* — and the depth it reports is what's actually holding the
lock, not the whole-book run's remaining chapters. Only chapters *asked for* are marked `⏳ queued`; a
chapter a bulk run will reach on its own keeps its Narrate button, because tapping it is how you pull
it forward past the rest of the run.

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
