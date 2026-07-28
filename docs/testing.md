# Tests

```bash
uv pip install --python .venv/bin/python pytest    # once
.venv/bin/python -m pytest                         # ~40 s
```

Also on every push and pull request, via `.github/workflows/tests.yml`.

## What's covered

Every module. The books state machine, which is where the bugs actually live: a render
cancelled under itself, what survives a restart, which chapters an export takes, how a part run
scopes its progress, the queue, what leaving a chapter out takes it out of, and which audio a changed
pronunciation invalidates — including a save landing mid-render. What a section put back as a chapter
renumbers and what it deliberately doesn't move, since a position and a filename mean different
things only there.

The pure functions — reading a chapter number out of a heading, cutting text into segments, chunks,
sentences and phoneme batches, what gets announced before the prose. The stores behind clips, presets
and chats. And the HTTP contracts, including the cache headers on narration audio.

Two things get more attention than their size suggests: `safe_path`, the app's only security
boundary, against climbing out, absolute paths, outward symlinks and shared-prefix siblings; and the
arrangement that gets an English answer out of a Dutch question, whose central rule is invisible from
the outside — the marker and the primer must never reach `chats.json`.

## What isn't

Anything you have to hear. Whether a pause is long enough, whether a voice reads a name right,
whether the lock screen looks right — no assertion tells you that.

The external engines aren't exercised either; each is stubbed at its boundary (`_render_segment`,
`run_stt`, `worker_call`, `ollama_models`) and everything above it is real, so what's tested is which
model gets asked for and what happens to the answer. `tests/test_export.py` is the exception and runs
ffmpeg for real, reading the chapter marks back out of a finished `.m4b` — the only way to know they
line up with the audio rather than merely being written.

## The frontend test

`tests/test_frontend.py` is structural, not behavioural: every `$("#id")` resolves, no element is
left unreferenced, tags balance, ids are unique, the inline script parses, and no download link is
built outside the one helper that knows what iOS does with them. One file with no build step means
nothing else catches a dangling reference before the phone does.

The parse check uses `node --check` where node exists, which is what CI runs, and falls back to
esprima where it doesn't — a desktop is not a build box, and the check is worth most on the machine
the page is edited on.

## Strictness and isolation

Tests needing a tool beyond python skip when it isn't installed, which is right on a laptop and wrong
in CI, where skipping is how a suite passes without having run. **`STRICT_TESTS=1`**, which CI sets,
refuses the run instead and names what's missing.

Each module keeps its storage paths in module globals, so tests point all nine — books, clips,
presets, outputs, samples, chats — at a tmpdir, autouse, and a second fixture fails any test that
changed real storage. A third refuses to start a speech engine, since `kokoro_voices()` reads like a
list lookup and is actually a subprocess launch plus a cache that would leak into every test after
it.

**Don't call `monkeypatch.undo()` in a test:** it reverts those redirects along with everything else,
and the rest of the test then runs against your own library.
