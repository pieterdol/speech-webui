# Working on this project

The README is the reference — how to run it, what each module owns, what the tests cover.
These are the three rules that don't show up by reading the code.

## Tests come with the change

Every behaviour change gets tests in the same commit: a new pure function, a new field on a
book, a new endpoint or a new branch in an existing one. `.venv/bin/python -m pytest` runs the
suite in about 15 seconds, so there's no reason to leave it for later.

Put them where the layer lives — `tests/test_text.py` and `tests/test_textprep.py` for the
pure functions, `tests/test_api.py` for HTTP contracts, `tests/test_render.py` for the state
machine — and name the test after the behaviour it protects, not the function it calls.

Some things genuinely can't be asserted: whether a pause is long enough, whether a voice says a
name right. Say so in the commit or the README rather than writing a test that fixes the wrong
answer in place.

## The README says what the app does, not how it got there

It's documentation, not a changelog or a lab notebook. A reader wants the current behaviour, the
settings, and the constraints that are still live — "Ollama needs these two variables", "Kokoro
has no Dutch voice". They don't want the bug that used to happen, the measurement that ruled an
alternative out, or how many occurrences of something turned up in one book.

So when a change lands, update the README to describe the new behaviour, and don't add the story
of finding it. Rationale is worth keeping when it stops someone undoing the decision — write it
as a present-tense constraint, not as an anecdote. The reasoning behind a specific piece of code
belongs in a comment next to that code, where it can't drift.

## No copyrighted text in the code

The books in `books/` are bought copies, and none of their prose belongs in this repository —
not in tests, not in fixtures, not quoted in a comment to show what a bug looked like. Write
your own sentence that has the same shape as the real one. Titles, authors and chapter names
are facts and are fine; passages aren't.

The same goes for anything else under copyright: song lyrics, poems, article text. A test needs
text with a particular *shape* — an abbreviation before a name, a date with slashes, a
paragraph long enough to split — and that's always cheaper to invent than to borrow.
