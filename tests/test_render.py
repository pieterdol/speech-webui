"""Rendering a chapter, and everything that can happen to it while it runs.

These are the paths that broke in practice: a render carrying on after its book was deleted,
a restart throwing away finished parts, a part run reporting the whole book's progress.
"""
import os
import threading
import time

import books


def files(book_id):
    d = books.book_dir(book_id, "audio")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def chapter(book_id, i=0):
    return books.find_book(book_id)["chapters"][i]


def delete_book(book_id):
    """What POST /api/books/delete does."""
    books.write_books([b for b in books.load_books() if b.get("id") != book_id])
    import shutil
    shutil.rmtree(books.book_dir(book_id), ignore_errors=True)


LONG = "\n".join(["word " * 200] * 40)          # splits into several segments


class TestRenderChapter:
    def test_marks_ready_and_keeps_every_part(self, make_book, fake_tts):
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        c = chapter("b1")
        assert c["state"] == "ready"
        assert len(c["segments"]) == len(files("b1")) > 1
        assert c["seconds"] > 0

    def test_publishes_the_part_count_before_the_first_part_exists(self, make_book, monkeypatch):
        """The queue panel says "part 1 of 8" from the start, which it can only do if the
        split happens before the state is written."""
        seen = []

        def slow_segment(text, voice, out_path, intro=None, tail_pause=0):
            seen.append(dict(books.find_book("b1")["chapters"][0]))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0")

        monkeypatch.setattr(books, "_render_segment", slow_segment)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        assert seen[0]["total"] > 1                 # known before any audio was made
        assert seen[0]["segments"] == []

    def test_each_part_is_published_as_it_finishes(self, make_book, monkeypatch):
        counts = []

        def watch(text, voice, out_path, intro=None, tail_pause=0):
            counts.append(len(books.find_book("b1")["chapters"][0]["segments"]))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0")

        monkeypatch.setattr(books, "_render_segment", watch)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        assert counts == list(range(len(counts)))   # 0,1,2… — one more visible each time

    def test_already_ready_is_left_alone(self, make_book, fake_tts):
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        made = len(fake_tts)
        books.render_chapter("b1", 0)
        assert len(fake_tts) == made

    def test_existing_parts_are_reused(self, make_book, fake_tts):
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        n = len(files("b1"))
        # put it back to pending as an interrupted render leaves it, minus the last part
        os.remove(books.book_dir("b1", "audio", f"ch000-s{n - 1:02d}.opus"))
        books.update_book("b1", lambda b: b["chapters"][0].update(state="pending"))
        fake_tts.clear()
        books.render_chapter("b1", 0)
        assert len(fake_tts) == 1                   # only the missing one was remade
        assert chapter("b1")["state"] == "ready"

    def test_missing_text_is_an_error_not_a_crash(self, make_book, fake_tts):
        make_book(texts=[LONG])
        os.remove(books.book_dir("b1", "text", "ch000.txt"))
        books.render_chapter("b1", 0)
        assert chapter("b1")["state"] == "error"
        assert "missing text" in chapter("b1")["error"]


class TestAnnouncementInvalidation:
    """The lead-in lives in a chapter's first part, and a resumed render reuses files already
    on disk — so the wording it was made with is recorded and checked."""

    def test_first_part_remade_when_the_wording_changes(self, make_book, fake_tts):
        make_book(names=["Chapter One"], texts=[LONG], announce=False)
        books.render_chapter("b1", 0)
        first = books.book_dir("b1", "audio", "ch000-s00.opus")
        before = os.path.getmtime(first)
        assert chapter("b1")["intro"] == []

        time.sleep(0.01)
        books.update_book("b1", lambda b: b.update(announce=True))
        books.update_book("b1", lambda b: b["chapters"][0].update(state="pending"))
        fake_tts.clear()
        books.render_chapter("b1", 0)

        assert chapter("b1")["intro"] == ["A Book", "by An Author", "1"]
        assert os.path.getmtime(first) > before          # s00 remade
        assert len(fake_tts) == 1                        # and only s00

    def test_unchanged_wording_reuses_everything(self, make_book, fake_tts):
        make_book(names=["Chapter One"], texts=[LONG])
        books.render_chapter("b1", 0)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="pending"))
        fake_tts.clear()
        books.render_chapter("b1", 0)
        assert fake_tts == []

    def test_the_wording_recorded_is_the_spoken_one(self, make_book, fake_tts):
        """Not the written one. "11/22/63" and "eleven, twenty-two, sixty-three" are the same
        title and different audio."""
        make_book(names=["Chapter One"], texts=[LONG], title="11/22/63", author="",
                  announce=True)
        books.render_chapter("b1", 0)
        assert chapter("b1")["intro"] == ["11, 22, 63", "1"]

    def test_a_pronunciation_change_makes_the_opening_stale(self, make_book, fake_tts):
        """The case a written-form record can't see: the title is untouched, only how it's said
        has changed, and the audio on disk is of the old pronunciation."""
        make_book(names=["Chapter One"], texts=[LONG], title="11/22/63", author="",
                  announce=True)
        books.render_chapter("b1", 0)
        first = books.book_dir("b1", "audio", "ch000-s00.opus")
        before = os.path.getmtime(first)

        time.sleep(0.01)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="pending", intro=["11/22/63", "1"]))       # what a rule ago would have said
        fake_tts.clear()
        books.render_chapter("b1", 0)

        assert os.path.getmtime(first) > before
        assert len(fake_tts) == 1                            # only the opening


class TestCancellation:
    def slow_tts(self, monkeypatch, delay=0.15):
        def _seg(text, voice, out_path, intro=None, tail_pause=0):
            time.sleep(delay)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
        monkeypatch.setattr(books, "_render_segment", _seg)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)

    def test_deleting_the_book_stops_it_and_leaves_nothing(self, make_book, monkeypatch):
        self.slow_tts(monkeypatch)
        make_book(texts=[LONG])
        t = threading.Thread(target=books.render_chapter, args=("b1", 0))
        t.start()
        time.sleep(0.35)
        delete_book("b1")
        t.join(timeout=20)
        assert not t.is_alive()
        assert not os.path.exists(books.book_dir("b1"))
        assert "b1" not in os.listdir(books.BOOKS_DIR)
        assert books.find_book("b1") is None

    def test_it_stops_within_a_segment_not_at_the_end(self, make_book, monkeypatch):
        made = []
        def _seg(text, voice, out_path, intro=None, tail_pause=0):
            made.append(out_path)
            time.sleep(0.15)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0")
        monkeypatch.setattr(books, "_render_segment", _seg)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        make_book(texts=[LONG])
        total = len(books.split_segments(LONG))
        assert total >= 4                             # otherwise the test proves nothing
        t = threading.Thread(target=books.render_chapter, args=("b1", 0))
        t.start()
        time.sleep(0.35)
        delete_book("b1")
        t.join(timeout=20)
        assert len(made) < total

    def test_narrator_change_resets_the_chapter_and_keeps_the_book(self, make_book, monkeypatch):
        self.slow_tts(monkeypatch)
        make_book(texts=[LONG])
        t = threading.Thread(target=books.render_chapter, args=("b1", 0))
        t.start()
        time.sleep(0.35)
        books.update_book("b1", lambda b: b.update(gen=b.get("gen", 0) + 1))
        t.join(timeout=20)
        assert books.find_book("b1") is not None
        assert chapter("b1")["state"] == "pending"
        assert chapter("b1")["segments"] == []
        assert files("b1") == []


class TestRestartRecovery:
    """A render dies with the process. What it had already finished is real audio."""

    def test_finished_parts_survive(self, make_book, fake_tts):
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        n = len(files("b1"))
        # a render in flight: state rendering, and the list emptied ready to be refilled
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="rendering", segments=[]))
        books.clear_stale_state()
        c = chapter("b1")
        assert c["state"] == "pending"
        assert len(c["segments"]) == n              # read back off disk, not from the index
        assert c["done"] == n

    def test_a_running_bulk_job_is_cleared(self, make_book):
        make_book(render_all={"running": True, "done": 1, "total": 4})
        books.clear_stale_state()
        assert books.find_book("b1")["render_all"]["running"] is False

    def test_untouched_books_are_left_alone(self, make_book, fake_tts):
        make_book(texts=[LONG])
        books.render_chapter("b1", 0)
        before = books.find_book("b1")
        books.clear_stale_state()
        assert books.find_book("b1") == before


class TestSegmentsOnDisk:
    def write(self, book_id, indexes):
        for si in indexes:
            p = books.book_dir(book_id, "audio", f"ch000-s{si:02d}.opus")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b"\0")

    def test_reads_them_in_order(self, make_book, monkeypatch):
        monkeypatch.setattr(books, "audio_seconds", lambda p: 3.0)
        make_book()
        self.write("b1", [0, 1, 2])
        got = books.segments_on_disk("b1", 0)
        assert [s["file"] for s in got] == ["ch000-s00.opus", "ch000-s01.opus", "ch000-s02.opus"]
        assert all(s["seconds"] == 3.0 for s in got)

    def test_stops_at_the_first_gap(self, make_book, monkeypatch):
        """Playback walks the parts in order, so a run that skips one is no run at all."""
        monkeypatch.setattr(books, "audio_seconds", lambda p: 3.0)
        make_book()
        self.write("b1", [0, 1, 3, 4])
        assert len(books.segments_on_disk("b1", 0)) == 2

    def test_missing_first_part_yields_nothing(self, make_book, monkeypatch):
        monkeypatch.setattr(books, "audio_seconds", lambda p: 3.0)
        make_book()
        self.write("b1", [1, 2])
        assert books.segments_on_disk("b1", 0) == []

    def test_a_half_written_last_file_is_dropped(self, make_book, monkeypatch):
        """ffprobe can't read a duration out of the file the process died writing."""
        make_book()
        self.write("b1", [0, 1, 2])
        bad = books.book_dir("b1", "audio", "ch000-s02.opus")
        monkeypatch.setattr(books, "audio_seconds", lambda p: 0.0 if p == bad else 3.0)
        got = books.segments_on_disk("b1", 0)
        assert len(got) == 2
        assert not os.path.exists(bad)              # and removed, so the retry remakes it

    def test_nothing_rendered(self, make_book):
        make_book()
        assert books.segments_on_disk("b1", 0) == []


class TestRenderAllWorker:
    def test_a_part_run_narrates_and_reports_only_that_part(self, make_book, fake_tts):
        names = [f"A · Chapter {i+1}" for i in range(3)] + [f"B · Chapter {i+1}" for i in range(7)]
        make_book(names=names, texts=["word " * 40] * 10)
        seen = []
        real_update = books.update_book

        def spy(book_id, fn):
            real_update(book_id, fn)
            ra = (books.find_book(book_id) or {}).get("render_all") or {}
            if ra.get("total") is not None:
                seen.append((ra.get("done"), ra["total"]))

        books.update_book = spy
        try:
            scope = books.chapters_in(books.find_book("b1"), "A")
            books.update_book("b1", lambda b: b.update(render_all={
                "running": True, "done": 0, "total": len(scope), "part": "A"}))
            books.render_all_worker("b1", "A")
        finally:
            books.update_book = real_update

        chapters = books.find_book("b1")["chapters"]
        assert [c["state"] for c in chapters[:3]] == ["ready"] * 3
        assert [c["state"] for c in chapters[3:]] == ["pending"] * 7
        assert {t for _d, t in seen} == {3}                  # never the book's 10
        assert books.find_book("b1")["render_all"]["running"] is False

    def test_a_whole_book_run_covers_everything(self, make_book, fake_tts):
        make_book(names=["Chapter One", "Chapter Two"], texts=["word " * 40] * 2)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2, "part": None}))
        books.render_all_worker("b1", None)
        assert all(c["state"] == "ready" for c in books.find_book("b1")["chapters"])

    def test_stopping_ends_it(self, make_book, fake_tts):
        make_book(names=[f"Chapter {i}" for i in range(5)], texts=["word " * 40] * 5)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": False, "done": 0, "total": 5}))
        books.render_all_worker("b1", None)
        assert all(c["state"] == "pending" for c in books.find_book("b1")["chapters"])

    def test_a_chapter_left_out_is_never_narrated(self, make_book, fake_tts):
        """Front matter the heuristics couldn't tell from prose, marked by hand. A whole-book
        run walks past it and the run still finishes."""
        make_book(names=["Other titles by this author", "One"], texts=["word " * 40] * 2)
        books.update_book("b1", lambda b: b["chapters"][0].update(skip=True))
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 1}))
        books.render_all_worker("b1", None)
        assert [c["state"] for c in books.find_book("b1")["chapters"]] \
            == ["pending", "ready"]
        assert books.find_book("b1")["render_all"]["running"] is False

    def test_an_errored_chapter_is_skipped_not_retried_forever(self, make_book, fake_tts):
        make_book(names=["One", "Two"], texts=["word " * 40] * 2)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="error"))
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2}))
        books.render_all_worker("b1", None)
        states = [c["state"] for c in books.find_book("b1")["chapters"]]
        assert states == ["error", "ready"]


class TestQueue:
    def test_idle(self):
        assert books.render_status() == {"current": None, "queue": []}

    def test_waiting_and_current(self, make_book, monkeypatch):
        make_book(names=["One", "Two", "Three"], texts=["word " * 40] * 3)
        started = threading.Event()
        release = threading.Event()

        def _seg(text, voice, out_path, intro=None, tail_pause=0):
            started.set()
            release.wait(timeout=10)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0")

        monkeypatch.setattr(books, "_render_segment", _seg)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)

        threads = [threading.Thread(target=books.render_chapter, args=("b1", i))
                   for i in range(3)]
        for t in threads:
            t.start()
        assert started.wait(timeout=10)
        time.sleep(0.2)                       # let the other two stack up behind the lock

        st = books.render_status()
        assert st["current"] is not None
        assert st["current"]["state"] == "narrating"
        assert len(st["queue"]) == 2
        assert all(e["state"] == "waiting" for e in st["queue"])

        release.set()
        for t in threads:
            t.join(timeout=20)
        assert books.render_status() == {"current": None, "queue": []}

    def test_a_chapter_asked_for_twice_is_one_line(self, make_book, monkeypatch):
        make_book(names=["One"], texts=["word " * 40])
        started = threading.Event()
        release = threading.Event()

        def _seg(text, voice, out_path, intro=None, tail_pause=0):
            started.set()
            release.wait(timeout=10)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0")

        monkeypatch.setattr(books, "_render_segment", _seg)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        threads = [threading.Thread(target=books.render_chapter, args=("b1", 0))
                   for _ in range(3)]
        for t in threads:
            t.start()
        assert started.wait(timeout=10)
        time.sleep(0.2)

        st = books.render_status()
        keys = [(e["book"], e["chapter"]) for e in st["queue"]]
        assert len(keys) == len(set(keys))
        assert ("b1", 0) not in keys          # it's the current one, not also queued

        release.set()
        for t in threads:
            t.join(timeout=20)

    def test_a_bulk_run_shows_what_it_has_yet_to_reach(self, make_book):
        make_book(names=[f"Chapter {i}" for i in range(4)], texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b.update(render_all={"running": True, "part": None}))
        st = books.render_status()
        assert [e["state"] for e in st["queue"]] == ["queued"] * 4

    def test_and_not_what_has_been_left_out(self, make_book):
        make_book(names=[f"Chapter {i}" for i in range(4)], texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b["chapters"][2].update(skip=True))
        books.update_book("b1", lambda b: b.update(render_all={"running": True, "part": None}))
        assert [e["chapter"] for e in books.render_status()["queue"]] == [0, 1, 3]
