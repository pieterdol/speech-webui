"""Shared fixtures.

speech.py keeps its paths in module-level globals (BOOKS_DIR, BOOKS_FILE) rather than in
config. That's what lets a test point the whole book layer at a tmpdir — and also what makes
forgetting to do so destructive, since the default is the real library. So the redirect is
autouse: no test can touch books.json by accident, including one that never asked.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import epub          # noqa: E402
import speech        # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

# Captured before anything redirects them: the developer's own library, which no test may
# touch. Recorded at import so it survives every monkeypatch in every test.
REAL_BOOKS_DIR = speech.BOOKS_DIR
REAL_BOOKS_FILE = speech.BOOKS_FILE


@pytest.fixture(autouse=True)
def real_library_untouched():
    """The backstop for the fixture below.

    A redirect that gets undone mid-test is silent — the test still passes, having written
    into the real books.json. So the real library is snapshotted around every test, and a
    test that so much as adds a directory to it fails.
    """
    def snapshot():
        listing = sorted(os.listdir(REAL_BOOKS_DIR)) if os.path.isdir(REAL_BOOKS_DIR) else []
        try:
            with open(REAL_BOOKS_FILE) as f:
                ids = sorted(b.get("id") for b in json.load(f))
        except (OSError, ValueError):
            ids = []
        return listing, ids

    # Which books exist, not when the file was last written: the dev server is usually up on
    # the same machine and rewrites the index after every chapter it narrates, so a timestamp
    # here fails at random. A test appending its own book changes the ids, which is the thing
    # actually worth catching.
    before = snapshot()
    yield
    assert snapshot() == before, "a test wrote into the real book library"


@pytest.fixture(autouse=True)
def isolated_books(tmp_path, monkeypatch):
    """Every test gets an empty library of its own.

    Do not call monkeypatch.undo() in a test: it reverts every patch on the shared
    function-scoped monkeypatch, this redirect included, and the rest of the test then runs
    against the real library. Ask for the narrower fixture you want instead.
    """
    books = tmp_path / "books"
    books.mkdir()
    monkeypatch.setattr(speech, "BOOKS_DIR", str(books))
    monkeypatch.setattr(speech, "BOOKS_FILE", str(tmp_path / "books.json"))
    return books


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
        speech.write_books(speech.load_books() + [book])
        os.makedirs(speech.book_dir(book_id, "text"), exist_ok=True)
        for i, t in enumerate(texts):
            with open(speech.book_dir(book_id, "text", f"ch{i:03d}.txt"), "w") as f:
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

    def _render_segment(text, voice, out_path, intro=None, tail_pause=0):
        calls.append({"text": text, "voice": voice, "out": out_path, "intro": intro,
                      "tail_pause": tail_pause})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"\0" * 256)

    monkeypatch.setattr(speech, "_render_segment", _render_segment)
    monkeypatch.setattr(speech, "audio_seconds", lambda p: 12.5)
    return calls


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
    speech.app.config["TESTING"] = True
    with speech.app.test_client() as c:
        yield c
