# Working on this project

The README is the short reference: what the app does, where to reach it, how to run it. The detail
sits in `docs/` — `books.md` (EPUB narration and export), `architecture.md` (modules, locks, resident
workers, settings), `gotchas.md` (this machine and its engines) and `testing.md` (what the suite
covers). These are the three rules that don't show up by reading the code.

## Tests come with the change

Every behaviour change gets tests in the same commit: a new pure function, a new field on a
book, a new endpoint or a new branch in an existing one. `.venv/bin/python -m pytest` runs the
suite in well under a minute, so there's no reason to leave it for later.

Put them where the layer lives — `tests/test_text.py` and `tests/test_textprep.py` for the
pure functions, `tests/test_api.py` for HTTP contracts, `tests/test_render.py` for the state
machine — and name the test after the behaviour it protects, not the function it calls.

Don't call `monkeypatch.undo()` in a test: the autouse fixtures point all nine storage paths at a
tmpdir, and undoing them runs the rest of the test against real storage.

Some things genuinely can't be asserted: whether a pause is long enough, whether a voice says a
name right. Say so in the commit or in `docs/testing.md` rather than writing a test that fixes the
wrong answer in place.

## The docs say what the app does, not how it got there

It's documentation, not a changelog or a lab notebook. A reader wants the current behaviour, the
settings, and the constraints that are still live — "Ollama needs these two variables", "Kokoro
has no Dutch voice". They don't want the bug that used to happen, the measurement that ruled an
alternative out, or how many occurrences of something turned up in one book.

So when a change lands, update the page that owns the behaviour — the README when it changes what
the app does or how you run it, otherwise the matching `docs/` file — and don't add the story of
finding it. The README stays scannable in a couple of minutes; detail that would grow it goes to
`docs/`. Rationale is worth keeping when it stops someone undoing the decision — write it as a
present-tense constraint, not as an anecdote. The reasoning behind a specific piece of code belongs
in a comment next to that code, where it can't drift.

## No copyrighted text in the code

The books in `books/` are bought copies, and none of their prose belongs in this repository —
not in tests, not in fixtures, not quoted in a comment to show what a bug looked like. Write
your own sentence that has the same shape as the real one. Titles, authors and chapter names
are facts and are fine; passages aren't.

The same goes for anything else under copyright: song lyrics, poems, article text. A test needs
text with a particular *shape* — an abbreviation before a name, a date with slashes, a
paragraph long enough to split — and that's always cheaper to invent than to borrow.
