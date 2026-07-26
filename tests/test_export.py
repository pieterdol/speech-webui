"""Building the .m4b, for real, with ffmpeg.

Everything else stubs the media out. This one doesn't: it makes actual opus parts, runs the
export, and reads the chapter marks back out of the file — which is the only way to know the
markers line up with the audio rather than merely being written somewhere.
"""
import json
import os
import subprocess

import pytest

import speech
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
        silence(speech.book_dir(book_id, "audio", name), seconds)
        segs.append({"file": name, "seconds": seconds})
    speech.update_book(book_id, lambda b: b["chapters"][index].update(
        state=state, segments=segs, seconds=seconds * parts))
    return segs


def run_export(book_id, part=None):
    jid = speech.new_job("export")
    speech.export_worker(jid, book_id, part)
    return speech.jobs[jid]


class TestWholeBook:
    def test_one_file_with_a_mark_per_chapter(self, make_book, silence):
        make_book(names=["Chapter One", "Chapter Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 2.0)
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        out = speech.book_dir("b1", "export", job["file"])
        assert os.path.exists(out)
        assert [m[2] for m in marks(out)] == ["Chapter One", "Chapter Two"]
        assert "2 chapters" in job["text"]

    def test_marks_follow_the_audio(self, make_book, silence):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 2.0)
        narrate("b1", 1, silence, 3.0)
        got = marks(speech.book_dir("b1", "export", run_export("b1")["file"]))
        assert got[0][0] == 0.0
        assert abs(got[0][1] - 2.0) < 0.3          # first chapter ends where the second starts
        assert abs(got[1][0] - 2.0) < 0.3
        assert abs(got[1][1] - 5.0) < 0.3

    def test_several_parts_become_one_chapter(self, make_book, silence):
        make_book(names=["Long One"], texts=["word " * 10])
        narrate("b1", 0, silence, 1.0, parts=3)
        got = marks(speech.book_dir("b1", "export", run_export("b1")["file"]))
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
        assert len(marks(speech.book_dir("b1", "export", job["file"]))) == 2
        assert "1 unfinished" in job["text"]
        assert "1 not narrated" in job["text"]

    def test_nothing_narrated_at_all_is_an_error(self, make_book):
        make_book(names=["One"], texts=["word " * 10])
        job = run_export("b1")
        assert job["status"] == "error"
        assert "nothing narrated" in job["error"]

    def test_a_chapter_whose_files_vanished_is_skipped(self, make_book, silence):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0)
        os.remove(speech.book_dir("b1", "audio", "ch001-s00.opus"))
        job = run_export("b1")
        assert job["status"] == "done", job.get("error")
        assert [m[2] for m in marks(speech.book_dir("b1", "export", job["file"]))] == ["One"]


class TestPartExport:
    def test_only_that_part(self, make_book, silence):
        names = ["A · Chapter 1", "A · Chapter 2", "B · Chapter 1"]
        make_book(names=names, texts=["word " * 10] * 3)
        for i in range(3):
            narrate("b1", i, silence, 1.0)
        job = run_export("b1", part="A")
        assert job["status"] == "done", job.get("error")
        assert len(marks(speech.book_dir("b1", "export", job["file"]))) == 2

    def test_the_part_prefix_is_dropped_from_the_marks(self, make_book, silence):
        """Inside a part export, "A · " on every marker is noise."""
        make_book(names=["A · Chapter 1", "A · Chapter 2"], texts=["word " * 10] * 2)
        narrate("b1", 0, silence, 1.0)
        narrate("b1", 1, silence, 1.0)
        job = run_export("b1", part="A")
        got = [m[2] for m in marks(speech.book_dir("b1", "export", job["file"]))]
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
        out = speech.book_dir("b1", "export", job["file"])
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
