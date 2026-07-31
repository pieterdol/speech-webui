"""Shared fixtures.

Each module keeps the paths it stores things under in module-level globals rather than in
config. That's what lets a test point a whole layer at a tmpdir — and also what makes
forgetting to do so destructive, since the default is the developer's own clips, chats and
books. So the redirect is autouse and covers every module: no test can reach real storage by
accident, including one that never asked.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import books        # noqa: E402
import cast         # noqa: E402
import chat         # noqa: E402
import clips        # noqa: E402
import core         # noqa: E402
import openlib      # noqa: E402
import tts          # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

# Tests that need something beyond python skip when it isn't there, which is right on a
# machine that hasn't got it and wrong in CI, where skipping is how a suite passes without
# having run. STRICT_TESTS=1 refuses the run instead.
STRICT_TOOLS = {"ffmpeg": "the export tests", "ffprobe": "the export tests",
                "node": "the page script parse check"}


def pytest_configure(config):
    if os.environ.get("STRICT_TESTS") != "1":
        return
    missing = {t: why for t, why in STRICT_TOOLS.items() if not shutil.which(t)}
    if missing:
        raise pytest.UsageError(
            "STRICT_TESTS=1 and these are not installed: "
            + ", ".join(f"{t} ({why})" for t, why in missing.items())
            + " — the suite would skip those tests and still pass.")

# Every place a module stores something, captured before anything redirects it. Recorded at
# import so it survives every monkeypatch in every test.
#
# (module, attribute, kind) — "dir" is created empty per test, "file" just gets a path that
# doesn't exist yet.
STORAGE = [
    (books, "BOOKS_DIR", "dir"),   (books, "BOOKS_FILE", "file"),
    (clips, "CLIPS_DIR", "dir"),   (clips, "PRESETS_DIR", "dir"),
    (clips, "INDEX_FILE", "file"), (clips, "PRESETS_FILE", "file"),
    (tts, "OUT_DIR", "dir"),       (tts, "SAMPLES_DIR", "dir"),
    (chat, "CHATS_FILE", "file"),
]
REAL = {(m.__name__, attr): getattr(m, attr) for m, attr, _kind in STORAGE}


@pytest.fixture(autouse=True)
def real_storage_untouched():
    """The backstop for the fixture below.

    A redirect that gets undone mid-test is silent — the test still passes, having written
    into the developer's own clips.json or books.json. So everything the app stores is
    snapshotted around every test, and a test that adds to any of it fails.

    It reports the damage rather than preventing it: by the time the assert runs the write
    has happened and has to be undone by hand. Loud and after the fact still beats a test
    suite that quietly edits your library for weeks.
    """
    def snapshot():
        state = {}
        for key, path in REAL.items():
            if os.path.isdir(path):
                state[key] = sorted(os.listdir(path))
            elif os.path.exists(path):
                # what's in it, not when it changed: the dev server is usually up on the same
                # machine rewriting these, so a timestamp would fail at random
                try:
                    with open(path) as f:
                        state[key] = sorted(x.get("id") for x in json.load(f))
                except (OSError, ValueError, AttributeError):
                    state[key] = "unreadable"
            else:
                state[key] = None
        return state

    before = snapshot()
    yield
    after = snapshot()
    changed = [k for k in before if before[k] != after[k]]
    assert not changed, f"a test wrote into real storage: {changed}"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Every test gets empty storage of its own, for every module that has any.

    Do not call monkeypatch.undo() in a test: it reverts every patch on the shared
    function-scoped monkeypatch, these redirects included, and the rest of the test then runs
    against your own clips and books. Ask for the narrower fixture you want instead.
    """
    # under a subdirectory, so tmp_path itself stays empty for tests that use it directly
    root = tmp_path / "_storage"
    root.mkdir()
    for module, attr, kind in STORAGE:
        target = root / f"{module.__name__}-{attr.lower()}"
        if kind == "dir":
            target.mkdir()
        monkeypatch.setattr(module, attr, str(target))
    return root


@pytest.fixture
def isolated_books(isolated_storage):
    """The book library's own directory, for tests that want to look inside it."""
    return isolated_storage / "books-books_dir"


@pytest.fixture(autouse=True)
def no_real_engines(monkeypatch):
    """No test starts a speech engine.

    kokoro_voices and piper_voices look like list lookups but each spawns a resident worker
    with its own interpreter and a few hundred MB of model, then caches the answer in a module
    global that would leak into every test after it.

    Replacing worker_call is what actually prevents that. The raise is only a signpost for
    anything calling it directly — kokoro_voices and piper_voices catch every exception and
    return [] by design, so through those two the effect is an empty voice list rather than a
    failure. That's the same path a machine without Piper installed takes, so it's honest; a
    test that wants voices patches kokoro_voices/piper_voices themselves.
    """
    def refuse(engine, payload, timeout=None):
        raise AssertionError(
            f"a test tried to start the {engine} engine — patch the function you meant to")

    monkeypatch.setattr(tts, "worker_call", refuse)
    monkeypatch.setattr(tts, "_voices", {})     # and no cache carried in from elsewhere


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches Open Library.

    Adding a book looks its description up in a thread, so without this every import test
    would go out to the internet — slowly, differently each time, and reporting a fault in
    someone else's service as a fault here. A test that wants a description patches
    openlib.describe itself.
    """
    def refuse(url):
        raise AssertionError(f"a test tried to fetch {url} — patch openlib.describe instead")

    monkeypatch.setattr(openlib, "_get", refuse)


@pytest.fixture
def make_book():
    """Write a book into the index with its chapter text on disk, the way an upload leaves it.

    `texts` is one string per chapter; `names` the chapter names, which carry the part prefix
    ("Part One · Chapter 2") exactly as epub.py joins them.
    """
    def _make(book_id="b1", names=("Chapter One",), texts=None, **extra):
        texts = texts or ["word " * 50] * len(names)
        chapters = [{"i": i, "name": n, "words": len(texts[i].split()),
                     "state": "pending", "segments": [], "seconds": None, "error": None}
                    for i, n in enumerate(names)]
        book = {"id": book_id, "title": "A Book", "author": "An Author", "language": "en",
                "voice": "af_heart", "gen": 0, "announce": False, "chapters": chapters}
        book.update(extra)
        books.write_books(books.load_books() + [book])
        os.makedirs(books.book_dir(book_id, "text"), exist_ok=True)
        for i, t in enumerate(texts):
            with open(books.book_dir(book_id, "text", f"ch{i:03d}.txt"), "w") as f:
                f.write(t)
        return book
    return _make


@pytest.fixture
def fake_tts(monkeypatch):
    """Replace the one function that actually costs GPU time.

    Everything above it — the segment loop, cancellation, the progress written to books.json —
    is what the book tests are about, and none of it needs a voice.
    """
    calls = []

    def _render_segment(text, voice, out_path, intro=None, tail_pause=0, respellings=None,
                        lang="", runs=None):
        calls.append({"text": text, "voice": voice, "out": out_path, "intro": intro,
                      "tail_pause": tail_pause, "respellings": respellings, "runs": runs})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"\0" * 256)

    monkeypatch.setattr(books, "_render_segment", _render_segment)
    monkeypatch.setattr(books, "audio_seconds", lambda p: 12.5)
    return calls


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """No test asks Ollama anything.

    cast.attribute takes an ask_fn for the per-window questions, but the pass at the end that asks
    which of the speakers are one person has no such seam — production always wants it — so on a
    machine where Ollama happens to be up it would answer for real, slowly and differently each
    time, and on one where it isn't it would quietly answer nothing. Answered "nothing to merge"
    here; a test about merging passes its own same_fn.
    """
    monkeypatch.setattr(cast, "same_person", lambda examples, **kw: {})


@pytest.fixture
def silence():
    """Make a real, playable opus file of n seconds — for the paths that run ffmpeg."""
    def _silence(path, seconds=1.0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", str(seconds), "-c:a", "libopus", "-b:a", "32k", path],
                       check=True, timeout=60)
        return path
    return _silence


@pytest.fixture
def client():
    core.app.config["TESTING"] = True
    with core.app.test_client() as c:
        yield c
