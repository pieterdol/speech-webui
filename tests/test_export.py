"""Building the .m4b, for real, with ffmpeg.

Everything else stubs the media out. This one doesn't: it makes actual opus parts, runs the
export, and reads the chapter marks back out of the file — which is the only way to know the
markers line up with the audio rather than merely being written somewhere.
"""
import json
import os
import subprocess

import books
import core
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def marks(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path],
                         capture_output=True, text=True, timeout=60).stdout
    return [(round(float(c["start_time"]), 1), round(float(c["end_time"]), 1),
             c["tags"]["title"]) for c in json.loads(out)["chapters"]]


def narrate(book_id, index, silence, seconds, parts=1, state="ready"):
    """Put real audio on disk for one chapter and record it the way a render would."""
    segs = []
    for si in range(parts):
        name = f"ch{index:03d}-s{si:02d}.opus"
        silence(books.book_dir(book_id, "audio", name), seconds)
        segs.append({"file": name, "seconds": seconds})
    books.update_book(book_id, lambda b: b["chapters"][index].update(
        state=state, segments=segs, seconds=seconds * parts))
    return segs


def run_export(book_id, part=None):
    jid = core.new_job("export")
    books.export_worker(jid, book_id, part)
    return core.jobs[jid]


class TestWholeBook:
    def test_one_file_with_a_mark_per_chapter(self, make_book, silence):
        make_book(names=["Chapter One", "Chapter Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 2.0)
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        out = books.book_dir("b1", "export", job["file"])
        assert os.path.exists(out)
        assert [m[2] for m in marks(out)] == ["Chapter One", "Chapter Two"]
        assert "2 chapters" in job["text"]

    def test_marks_follow_the_audio(self, make_book, silence):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 2.0)
        narrate("b1", 1, silence, 3.0)
        got = marks(books.book_dir("b1", "export", run_export("b1")["file"]))
        assert got[0][0] == 0.0
        assert abs(got[0][1] - 2.0) < 0.3          # first chapter ends where the second starts
        assert abs(got[1][0] - 2.0) < 0.3
        assert abs(got[1][1] - 5.0) < 0.3

    def test_several_parts_become_one_chapter(self, make_book, silence):
        make_book(names=["Long One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0, parts=3)
        got = marks(books.book_dir("b1", "export", run_export("b1")["file"]))
        assert len(got) == 1
        assert abs(got[0][1] - 3.0) < 0.3


class TestWhatGetsIncluded:
    def test_an_unfinished_chapter_is_included_and_counted(self, make_book, silence):
        """A chapter cut short still has real audio in it. Leaving it out is what made an
        export of a book in progress fail with "nothing narrated yet"."""
        make_book(names=["One", "Two", "Three"], texts=["word " * 10] * 3)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0, parts=2, state="pending")     # interrupted
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        assert len(marks(books.book_dir("b1", "export", job["file"]))) == 2
        assert "1 unfinished" in job["text"]
        assert "1 not narrated" in job["text"]

    def test_a_chapter_left_out_stays_out_of_the_file(self, make_book, silence):
        """Its audio is still on disk — leaving a chapter out is a mark, not a delete — but a
        download is the book, and it isn't part of the book any more."""
        make_book(names=["Other titles by this author", "One", "Two"],
                  texts=["word " * 10] * 3)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0)
        narrate("b1", 2, silence, 1.0)
        books.update_book("b1", lambda b: b["chapters"][0].update(skip=True))
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        assert [m[2] for m in marks(books.book_dir("b1", "export", job["file"]))] \
            == ["One", "Two"]
        assert os.path.exists(books.book_dir("b1", "audio", "ch000-s00.opus"))

    def test_nothing_narrated_at_all_is_an_error(self, make_book):
        make_book(names=["One"], texts=["word " * 10])
        job = run_export("b1")
        assert job["status"] == "error"
        assert "nothing narrated" in job["error"]

    def test_a_chapter_whose_files_vanished_is_skipped(self, make_book, silence):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0)
        os.remove(books.book_dir("b1", "audio", "ch001-s00.opus"))
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        assert [m[2] for m in marks(books.book_dir("b1", "export", job["file"]))] == ["One"]


class TestPartExport:
    def test_only_that_part(self, make_book, silence):
        names = ["A · Chapter 1", "A · Chapter 2", "B · Chapter 1"]
        make_book(names=names, texts=["word " * 10] * 3)
        for i in range(3):
            narrate("b1", i, silence, 1.0)
        job = run_export("b1", part="A")
        assert job["status"] == "done", job.get("error")
        assert len(marks(books.book_dir("b1", "export", job["file"]))) == 2

    def test_the_part_prefix_is_dropped_from_the_marks(self, make_book, silence):
        """Inside a part export, "A · " on every marker is noise."""
        make_book(names=["A · Chapter 1", "A · Chapter 2"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0)
        job = run_export("b1", part="A")
        got = [m[2] for m in marks(books.book_dir("b1", "export", job["file"]))]
        assert got == ["Chapter 1", "Chapter 2"]

    def test_unknown_part(self, make_book, silence):
        make_book(names=["A · Chapter 1"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1", part="Nope")
        assert job["status"] == "error"
        assert "Nope" in job["error"]


class TestMetadata:
    def test_title_author_and_album(self, make_book, silence):
        make_book(names=["One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1")
        out = books.book_dir("b1", "export", job["file"])
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", out],
                               capture_output=True, text=True, timeout=60).stdout
        tags = {k.lower(): v for k, v in json.loads(probe)["format"]["tags"].items()}
        assert tags["title"] == "A Book"
        assert tags["album"] == "A Book"
        assert tags["artist"] == "An Author"

    def test_a_part_export_is_titled_for_the_part(self, make_book, silence):
        make_book(names=["A · Chapter 1"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1", part="A")
        assert "A" in job["file"]

    def test_the_download_url_is_encoded(self, make_book, silence):
        """The filename keeps its spaces — it's what the player shows — so the URL can't."""
        make_book(names=["One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1")
        assert " " in job["file"]
        assert " " not in job["url"]
        assert "%20" in job["url"]


class TestNothingHalfBuiltIsOffered:
    """The export is encoded under a name the library doesn't recognise and renamed when it's
    whole. Writing straight to the .m4b put a file that grew as you watched into "Exported
    audiobooks", with Share and Delete beside however much of an audiobook existed so far."""

    def test_the_finished_file_is_the_only_one_left(self, make_book, silence):
        make_book(names=["One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        left = sorted(os.listdir(books.book_dir("b1", "export")))
        assert job["file"] in left
        assert not [f for f in left if f.endswith(".part")]

    def test_it_is_encoded_under_the_part_name(self, make_book, silence, monkeypatch):
        """What the listing never sees. Caught by watching the file ffmpeg is handed rather than
        by trusting the name it ends up under."""
        make_book(names=["One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        real = subprocess.run
        handed = []

        def spy(cmd, *a, **kw):
            if cmd and cmd[0] == "ffmpeg":
                handed.append(cmd[-1])
            return real(cmd, *a, **kw)

        monkeypatch.setattr(books.subprocess, "run", spy)
        run_export("b1")
        assert handed and handed[-1].endswith(".m4b.part")
        assert books.book_exports("b1")[0]["file"].endswith(".m4b")

    def test_a_partial_file_is_never_listed(self, make_book):
        """Directly: whatever the encoder is doing, the listing is what the page offers."""
        make_book()
        p = books.book_dir("b1", "export", "A Book.m4b.part")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 4096)
        assert books.book_exports("b1") == []

    def test_a_failed_encode_leaves_nothing_behind(self, make_book, silence, monkeypatch):
        make_book(names=["One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0)
        real = subprocess.run

        def half_write(cmd, *a, **kw):
            if cmd and cmd[0] == "ffmpeg" and cmd[-1].endswith(".part"):
                open(cmd[-1], "wb").write(b"\0" * 2048)      # started, then died
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            return real(cmd, *a, **kw)

        monkeypatch.setattr(books.subprocess, "run", half_write)
        job = run_export("b1")
        assert job["status"] == "error"
        assert os.listdir(books.book_dir("b1", "export")) == []

    def test_a_restart_sweeps_one_left_by_a_killed_export(self, make_book):
        make_book()
        p = books.book_dir("b1", "export", "A Book.m4b.part")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 4096)
        keep = books.book_dir("b1", "export", "Older.m4b")
        open(keep, "wb").write(b"\0" * 128)
        books.clear_stale_state()
        assert os.listdir(books.book_dir("b1", "export")) == ["Older.m4b"]


class TestWhatAnExportSaysAboutItself:
    """The chapter counts and the running time were in the job's result and nowhere else, so
    they vanished on the next reload while the file they described stayed. They're written
    beside the file now, which is what lets one list serve both the export just built and the
    ones built yesterday."""

    def test_written_beside_the_file(self, make_book, silence):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        job = run_export("b1")
        got = books.book_exports("b1")[0]
        assert got["text"] == job["text"] == "1 chapters, 1 not narrated"
        assert got["seconds"] == job["seconds"]

    def test_an_export_without_one_still_lists(self, make_book):
        """Built before the note existed — it just says less about itself."""
        make_book()
        p = books.book_dir("b1", "export", "Old.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 512)
        got = books.book_exports("b1")[0]
        assert got["file"] == "Old.m4b"
        assert got["text"] is None and got["seconds"] is None

    def test_the_note_is_not_offered_as_an_audiobook(self, make_book):
        make_book()
        p = books.book_dir("b1", "export", "A Book.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 512)
        books.write_export_note("b1", "A Book.m4b", "1 chapters", 12.5)
        assert [e["file"] for e in books.book_exports("b1")] == ["A Book.m4b"]

    def test_deleting_the_export_takes_its_note(self, client, make_book):
        make_book()
        p = books.book_dir("b1", "export", "A Book.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 512)
        books.write_export_note("b1", "A Book.m4b", "1 chapters", 12.5)
        client.post("/api/books/export/delete", json={"id": "b1", "file": "A Book.m4b"})
        assert os.listdir(books.book_dir("b1", "export")) == []
