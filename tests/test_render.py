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

        def slow_segment(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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

        def watch(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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

    def test_a_titled_chapter_says_its_title_once_with_a_pause_after_it(self, make_book,
                                                                        fake_tts):
        """A book that names its chapters has the name in the prose as well, as the heading
        line. Read from there it runs straight into the text; announced, it gets real silence
        after it — and the heading comes out of the text so it isn't said twice."""
        make_book(names=["Palancar Valley"], texts=["PALANCAR VALLEY\n" + LONG], announce=True)
        books.render_chapter("b1", 0)
        intro = fake_tts[0]["intro"]
        assert intro[-1][0] == "Palancar Valley" and intro[-1][1] > 0
        assert "PALANCAR VALLEY" not in fake_tts[0]["text"]

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
        def _seg(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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
        def _seg(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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
            books.render_all_worker("b1")
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
        books.render_all_worker("b1")
        assert all(c["state"] == "ready" for c in books.find_book("b1")["chapters"])

    def test_stopping_ends_it(self, make_book, fake_tts):
        make_book(names=[f"Chapter {i}" for i in range(5)], texts=["word " * 40] * 5)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": False, "done": 0, "total": 5}))
        books.render_all_worker("b1")
        assert all(c["state"] == "pending" for c in books.find_book("b1")["chapters"])

    def test_a_chapter_left_out_is_never_narrated(self, make_book, fake_tts):
        """Front matter the heuristics couldn't tell from prose, marked by hand. A whole-book
        run walks past it and the run still finishes."""
        make_book(names=["Other titles by this author", "One"], texts=["word " * 40] * 2)
        books.update_book("b1", lambda b: b["chapters"][0].update(skip=True))
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 1}))
        books.render_all_worker("b1")
        assert [c["state"] for c in books.find_book("b1")["chapters"]] \
            == ["pending", "ready"]
        assert books.find_book("b1")["render_all"]["running"] is False

    def test_an_errored_chapter_is_skipped_not_retried_forever(self, make_book, fake_tts):
        make_book(names=["One", "Two"], texts=["word " * 40] * 2)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="error"))
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2}))
        books.render_all_worker("b1")
        states = [c["state"] for c in books.find_book("b1")["chapters"]]
        assert states == ["error", "ready"]


class TestStorageNumbers:
    """A chapter's position is its number to everything else, and used to be its filename too.
    Those part company the moment a section is put back into the middle of a book, so what a
    render, a repair and a rescan address files by is the number the chapter was made with."""

    def keyed(self, make_book, text="word " * 40):
        """A book whose second chapter has moved along without its files moving — what putting a
        section back in front of it leaves behind."""
        make_book(names=["One", "Two"], texts=[text, text])
        books.update_book("b1", lambda b: b["chapters"][1].update(key=7))
        with open(books.text_file("b1", 7), "w") as f:
            f.write(text)
        return books.find_book("b1")

    def test_renumber_keeps_the_storage_number_and_drops_it_where_it_agrees(self):
        chapters = [{"i": 0, "key": 4}, {"i": 1}, {"i": 2, "key": 9}]
        books.renumber([chapters[2], chapters[0], chapters[1]])
        assert chapters[2] == {"i": 0, "key": 9}
        assert chapters[0] == {"i": 1, "key": 4}
        # no key of its own means its files are under the position it used to hold, and that's
        # what gets written down now the two have parted company
        assert chapters[1] == {"i": 2, "key": 1}

    def test_a_key_that_becomes_its_position_again_is_dropped(self):
        chapters = [{"i": 0, "key": 1}, {"i": 1, "key": 0}]
        books.renumber([chapters[1], chapters[0]])
        assert chapters == [{"i": 1}, {"i": 0}]

    def test_a_book_nothing_was_inserted_into_carries_no_keys(self, make_book):
        make_book(names=["One", "Two"])
        books.renumber(books.find_book("b1")["chapters"])
        assert all("key" not in c for c in books.find_book("b1")["chapters"])

    def test_the_text_is_read_from_the_chapters_own_number(self, make_book):
        book = self.keyed(make_book, "the notice itself. ")
        assert books.chapter_segments(book, 1) == ["the notice itself."]

    def test_a_render_writes_under_the_chapters_own_number(self, make_book, fake_tts):
        self.keyed(make_book)
        books.render_chapter("b1", 1)
        c = chapter("b1", 1)
        assert c["state"] == "ready"
        assert [s["file"] for s in c["segments"]] == ["ch007-s00.opus"]
        assert files("b1") == ["ch007-s00.opus"]

    def test_a_repair_deletes_the_part_that_belongs_to_the_chapter(self, make_book, fake_tts):
        """Keyed by position, a pronunciation change would delete whatever part happened to be
        called ch001-s00 — another chapter's, once anything has been put back."""
        self.keyed(make_book, "Vermeer said so. ")
        books.render_chapter("b1", 1)
        assert files("b1") == ["ch007-s00.opus"]
        plan = books.respell_repair_plan(books.find_book("b1"), {}, {"Vermeer": "Vermayr"})
        assert plan == {1: [0]}
        books.apply_respell_repair("b1", plan)
        assert files("b1") == []
        assert chapter("b1", 1)["state"] == "pending"

    def test_what_a_restart_finds_is_looked_up_by_the_same_number(self, make_book, monkeypatch):
        self.keyed(make_book)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 4.0)
        os.makedirs(books.book_dir("b1", "audio"), exist_ok=True)
        with open(books.audio_file("b1", 7, 0), "wb") as f:
            f.write(b"\0" * 16)
        books.update_book("b1", lambda b: b["chapters"][1].update(state="rendering", segments=[]))
        books.clear_stale_state()
        assert chapter("b1", 1)["segments"] == [{"file": "ch007-s00.opus", "seconds": 4.0}]

    def test_narrating_it_again_from_nothing_finds_its_own_parts(self, make_book, fake_tts):
        self.keyed(make_book)
        books.render_chapter("b1", 1)
        books.render_chapter("b1", 0)
        books.drop_chapter_audio("b1", 1)
        assert files("b1") == ["ch000-s00.opus"]        # and not the other chapter's
        assert chapter("b1", 1)["segments"] == []


class TestAutoExport:
    """The point of narrating overnight is an .m4b in the morning. A per-book toggle builds it
    at the end of a run, so the run finishing and the file existing aren't two taps apart."""

    @staticmethod
    def exports(monkeypatch):
        """What export_worker was asked to build, without running an hour of ffmpeg."""
        built = []
        monkeypatch.setattr(books, "export_worker",
                            lambda jid, book_id, part=None: built.append((book_id, part)))
        return built

    def test_a_finished_run_exports_the_book(self, make_book, fake_tts, monkeypatch):
        built = self.exports(monkeypatch)
        make_book(names=["One", "Two"], texts=["word " * 40] * 2, auto_export=True)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2}))
        books.render_all_worker("b1")
        assert built == [("b1", None)]

    def test_a_book_that_did_not_ask_gets_nothing(self, make_book, fake_tts, monkeypatch):
        built = self.exports(monkeypatch)
        make_book(names=["One"], texts=["word " * 40])
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 1}))
        books.render_all_worker("b1")
        assert built == []

    def test_a_stopped_run_exports_nothing(self, make_book, fake_tts, monkeypatch):
        """Stopping is the answer to "not like this" — half a book is not what you asked for,
        and the chapters left pending say so."""
        built = self.exports(monkeypatch)
        make_book(names=["One", "Two"], texts=["word " * 40] * 2, auto_export=True)
        books.update_book("b1", lambda b: b.update(render_all={"running": False, "total": 2}))
        books.render_all_worker("b1")
        assert built == []

    def test_a_one_part_run_exports_that_part(self, make_book, fake_tts, monkeypatch):
        built = self.exports(monkeypatch)
        make_book(names=["A · One", "B · One"], texts=["word " * 40] * 2, auto_export=True)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 1, "parts": ["A"]}))
        books.render_all_worker("b1")
        assert built == [("b1", "A")]

    def test_a_run_over_several_parts_exports_the_whole_book(self, make_book, fake_tts,
                                                             monkeypatch):
        """One file rather than one per part: a run widened to two parts is on its way to
        covering the book, and the whole book is the file you'd ask for by hand."""
        built = self.exports(monkeypatch)
        make_book(names=["A · One", "B · One", "C · One"], texts=["word " * 40] * 3,
                  auto_export=True)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2, "parts": ["A", "B"]}))
        books.render_all_worker("b1")
        assert built == [("b1", None)]

    def test_a_failed_chapter_does_not_hold_the_export_back(self, make_book, fake_tts,
                                                            monkeypatch):
        """A run steps past an error, so "nothing pending left" is as finished as it gets —
        and the export says how many chapters were unfinished or missing anyway."""
        built = self.exports(monkeypatch)
        make_book(names=["One", "Two"], texts=["word " * 40] * 2, auto_export=True)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="error"))
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2}))
        books.render_all_worker("b1")
        assert built == [("b1", None)]


class TestQueue:
    def test_idle(self):
        assert books.render_status() == {"current": None, "queue": []}

    def test_waiting_and_current(self, make_book, monkeypatch):
        make_book(names=["One", "Two", "Three"], texts=["word " * 40] * 3)
        started = threading.Event()
        release = threading.Event()

        def _seg(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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

        def _seg(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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


class TestRenderDepth:
    """What the page tells the reader when they tap a chapter: it either starts narrating or
    joins a queue, and since renders are serialized across every book those two look identical
    for the next twenty minutes."""

    def test_idle(self):
        assert books.render_depth() == 0

    def test_counts_the_one_narrating_and_the_ones_behind_it(self, make_book, monkeypatch):
        make_book(names=["One", "Two", "Three"], texts=["word " * 40] * 3)
        started = threading.Event()
        release = threading.Event()

        def _seg(text, voice, out_path, intro=None, tail_pause=0, respellings=None):
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

        assert books.render_depth() == 3      # one narrating, two waiting

        release.set()
        for t in threads:
            t.join(timeout=20)
        assert books.render_depth() == 0

    def test_a_bulk_run_counts_as_the_chapter_it_is_on(self, make_book):
        """render_status projects the rest of a whole-book run so the page can list what's
        coming; the depth must not, or a chapter tapped during a run would be told it is 190th
        in line when it is in fact second."""
        make_book(names=[f"Chapter {i}" for i in range(4)], texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b.update(render_all={"running": True, "part": None}))
        assert len(books.render_status()["queue"]) == 4
        assert books.render_depth() == 0      # nothing is holding the lock yet


class TestARunThatGrows:
    """The worker takes no scope of its own: it reads what the run covers from the book between
    chapters, so a part added while it's going is picked up by the worker already in flight."""

    def test_it_narrates_a_part_added_after_it_started(self, make_book, fake_tts):
        names = [f"A · Chapter {i}" for i in range(2)] + [f"B · Chapter {i}" for i in range(2)]
        make_book(names=names, texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2, "parts": ["A"]}))

        # add B the way the endpoint does, from inside the run: after the first chapter of A
        real = books.render_chapter
        added = []

        def render_then_add(book_id, i):
            real(book_id, i)
            if not added:
                added.append(i)
                books.update_book("b1", lambda b: b["render_all"].update(parts=["A", "B"]))

        books.render_chapter = render_then_add
        try:
            books.render_all_worker("b1")
        finally:
            books.render_chapter = real

        assert [c["state"] for c in books.find_book("b1")["chapters"]] == ["ready"] * 4
        assert books.find_book("b1")["render_all"]["total"] == 4      # recounted as it widened

    def test_a_part_run_still_stops_at_its_own_part(self, make_book, fake_tts):
        """The other half of the same behaviour: reading the slot must not quietly widen a run
        that nobody widened."""
        names = [f"A · Chapter {i}" for i in range(2)] + [f"B · Chapter {i}" for i in range(2)]
        make_book(names=names, texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2, "parts": ["A"]}))
        books.render_all_worker("b1")
        assert [c["state"] for c in books.find_book("b1")["chapters"]] \
            == ["ready", "ready", "pending", "pending"]

    def test_the_queue_panel_lists_every_part_the_run_covers(self, make_book):
        names = [f"A · Chapter {i}" for i in range(2)] + [f"B · Chapter {i}" for i in range(2)]
        make_book(names=names, texts=["word " * 40] * 4)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "parts": ["A", "B"]}))
        assert len(books.render_status()["queue"]) == 4
        books.update_book("b1", lambda b: b["render_all"].update(parts=["A"]))
        assert len(books.render_status()["queue"]) == 2


class TestABooksOwnPronunciations:
    """A book's map has to reach the engine and be recorded in what the audio was made with."""

    def test_the_map_reaches_the_segment_render(self, make_book, fake_tts):
        make_book(names=["One"], texts=["word " * 40], respell={"Vermeer": "Vermayr"})
        books.render_chapter("b1", 0)
        assert fake_tts[0]["respellings"] == {"Vermeer": "Vermayr"}

    def test_a_book_without_one_passes_nothing(self, make_book, fake_tts):
        make_book(names=["One"], texts=["word " * 40])
        books.render_chapter("b1", 0)
        assert fake_tts[0]["respellings"] == {}

    def test_the_recorded_opening_is_respelled_through_it(self, make_book, fake_tts):
        """The intro record is what makes a stale opening detectable, so it has to be the
        book's own spoken form, not the global one."""
        make_book(names=["Chapter One"], texts=["word " * 40], title="Vermeer", author="",
                  announce=True, respell={"Vermeer": "Vermayr"})
        books.render_chapter("b1", 0)
        assert chapter("b1")["intro"] == ["Vermayr", "1"]

    def test_the_split_does_not_move_with_the_map(self, make_book, fake_tts):
        """Segment count decides every filename. If respelling happened before the split, a
        longer replacement could add a segment and rename everything after it — one word would
        invalidate the whole book."""
        text = "\n".join(f"Vermeer painted this one, number {i}. " * 30 for i in range(12))
        make_book(names=["One"], texts=[text])
        books.render_chapter("b1", 0)
        plain = [c["out"] for c in fake_tts]

        books.update_book("b1", lambda b: b.update(respell={"Vermeer": "V" * 60}))
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="pending", segments=[]))
        for f in os.listdir(books.book_dir("b1", "audio")):
            os.remove(books.book_dir("b1", "audio", f))
        fake_tts.clear()
        books.render_chapter("b1", 0)
        assert [c["out"] for c in fake_tts] == plain
        assert len(plain) > 1                     # a real multi-segment chapter, or it proves nothing


class TestFindingTheAudioAMapChangeInvalidates:
    """The scan that decides what gets re-narrated. Everything here is text work — no audio is
    made — and it has to agree with the render about what each segment contains."""

    def long_text(self, per_para=30, paras=12, word="Vermeer"):
        """Enough prose to cut into several segments, with the word only in the first."""
        first = f"{word} painted this. " * per_para
        rest = "\n".join("Nothing of interest happened here at all. " * per_para
                         for _ in range(paras))
        return first + "\n" + rest

    def test_a_word_in_one_segment_marks_only_that_segment(self, make_book):
        make_book(names=["One"], texts=[self.long_text()])
        book = self.narrated()
        assert len(books.chapter_segments(book, 0)) > 2        # or it proves nothing
        assert books.stale_segments(book, 0, {}, {"Vermeer": "Vermayr"}) == {0}

    def test_a_word_in_every_segment_marks_all_of_them(self, make_book):
        make_book(names=["One"], texts=[self.long_text(word="Nothing")])
        book = self.narrated()
        n = len(books.chapter_segments(book, 0))
        assert books.stale_segments(book, 0, {}, {"nothing": "nuthin"}) == set(range(n))

    def narrated(self, book_id="b1", index=0, intro=None):
        """A rendered chapter carries the lead-in it was made with; an un-narrated one has no
        record at all. Only the first is comparable, so the tests below say which they mean."""
        books.update_book(book_id, lambda b: b["chapters"][index].update(intro=intro or []))
        return books.find_book(book_id)

    def test_a_word_that_is_not_there_marks_nothing(self, make_book):
        make_book(names=["One"], texts=[self.long_text()])
        book = self.narrated()
        assert books.stale_segments(book, 0, {}, {"Rembrandt": "Rembrant"}) == set()

    def test_a_chapter_with_no_recorded_opening_is_taken_as_stale(self, make_book):
        """Only a rendered chapter has the record, so absent means "can't tell" — the same
        reading render_chapter takes before it deletes the opening. It costs nothing on a
        chapter with no audio, which is the only kind that has no record."""
        make_book(names=["One"], texts=[self.long_text()])
        book = books.find_book("b1")
        assert "intro" not in book["chapters"][0]
        assert books.stale_segments(book, 0, {}, {"Rembrandt": "Rembrant"}) == {0}
        assert books.respell_repair_plan(book, {}, {"Rembrandt": "Rembrant"}) == {}

    def test_removing_an_entry_invalidates_the_same_audio(self, make_book):
        """The audio still says the respelled form, so it's as wrong as adding one."""
        make_book(names=["One"], texts=[self.long_text()])
        book = self.narrated()
        assert books.stale_segments(book, 0, {"Vermeer": "Vermayr"}, {}) == {0}

    def test_editing_one_invalidates_it_too(self, make_book):
        make_book(names=["One"], texts=[self.long_text()])
        book = self.narrated()
        assert books.stale_segments(book, 0, {"Vermeer": "Vermayr"},
                                    {"Vermeer": "Fermeer"}) == {0}

    def test_a_word_only_in_the_title_marks_the_opening(self, make_book):
        """Via the recorded lead-in, which is what catches the title, the author and a part
        name without any of them being named in the scan."""
        make_book(names=["Chapter One"], texts=[self.long_text(word="Nobody")],
                  title="Vermeer", author="", announce=True)
        books.update_book("b1", lambda b: b["chapters"][0].update(intro=["Vermeer", "1"]))
        book = books.find_book("b1")
        assert books.stale_segments(book, 0, {}, {"Vermeer": "Vermayr"}) == {0}

    def test_a_word_in_the_heading_line_marks_nothing(self, make_book):
        """The heading is dropped before the text is read, so a word only there is never
        spoken — and re-narrating over it would be work for no change."""
        make_book(names=["Vermeer"], texts=["Vermeer\n" + "Something else happened. " * 30])
        book = self.narrated()
        assert "Vermeer" in open(books.book_dir("b1", "text", "ch000.txt")).read()
        assert books.stale_segments(book, 0, {}, {"Vermeer": "Vermayr"}) == set()

    def test_a_chapter_with_no_text_file_is_skipped_not_fatal(self, make_book):
        make_book(names=["One"], texts=["word " * 40])
        os.remove(books.book_dir("b1", "text", "ch000.txt"))
        book = books.find_book("b1")
        assert books.chapter_segments(book, 0) == []
        assert books.respell_repair_plan(book, {}, {"word": "werd"}) == {}

    def test_the_plan_covers_only_audio_that_exists(self, make_book, fake_tts):
        make_book(names=["One", "Two"], texts=[self.long_text()] * 2)
        books.render_chapter("b1", 0)                          # chapter two stays un-narrated
        book = books.find_book("b1")
        assert books.respell_repair_plan(book, {}, {"Vermeer": "Vermayr"}) == {0: [0]}

    def test_an_identical_map_plans_nothing(self, make_book, fake_tts):
        make_book(names=["One"], texts=[self.long_text()])
        books.render_chapter("b1", 0)
        book = books.find_book("b1")
        assert books.respell_repair_plan(book, {"a": "b"}, {"a": "b"}) == {}

    def test_the_segment_indices_are_the_render_s_own(self, make_book, fake_tts):
        """Whatever the scan calls segment 2 has to be the file the render calls segment 2."""
        make_book(names=["One"], texts=[self.long_text(word="Nothing")])
        books.render_chapter("b1", 0)
        book = books.find_book("b1")
        plan = books.respell_repair_plan(book, {}, {"nothing": "nuthin"})
        made = [os.path.basename(c["out"]) for c in fake_tts]
        assert [f"ch000-s{si:02d}.opus" for si in plan[0]] == made


class TestAMapChangeDuringARender:
    """A render reads the book's map once and then holds the lock for the whole chapter, so a
    respelling saved half way through is missing from every part after it. The chapter must not
    be marked ready in that state."""

    def paragraphs(self, word="Vermeer", at=1, n=4):
        """One paragraph per segment, the word in exactly one of them."""
        filler = "Nothing of interest happened here. " * 200
        return "\n".join((word + " " + filler) if i == at else filler for i in range(n))

    def test_it_is_left_pending_rather_than_ready(self, make_book, monkeypatch):
        make_book(names=["One"], texts=[self.paragraphs()])
        seen = []

        def save_the_map_midway(text, voice, out_path, intro=None, tail_pause=0,
                                respellings=None):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
            seen.append(os.path.basename(out_path))
            if len(seen) == 1:                 # a save landing while part two is still to come
                books.update_book("b1", lambda b: b.update(respell={"Vermeer": "Vermayr"}))

        monkeypatch.setattr(books, "_render_segment", save_the_map_midway)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        books.render_chapter("b1", 0)

        c = chapter("b1")
        assert c["state"] == "pending"                       # not ready, though it got to the end
        assert not os.path.exists(books.book_dir("b1", "audio", "ch000-s01.opus"))

    def test_the_parts_it_made_correctly_are_kept(self, make_book, monkeypatch):
        """Only the ones the change invalidates go — the rest were made with a map that still
        holds, and re-making them would be an hour of work for no difference."""
        make_book(names=["One"], texts=[self.paragraphs()])
        made = []

        def save_the_map_midway(text, voice, out_path, intro=None, tail_pause=0,
                                respellings=None):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
            made.append(os.path.basename(out_path))
            if len(made) == 1:
                books.update_book("b1", lambda b: b.update(respell={"Vermeer": "Vermayr"}))

        monkeypatch.setattr(books, "_render_segment", save_the_map_midway)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        books.render_chapter("b1", 0)

        assert files("b1") == ["ch000-s00.opus", "ch000-s02.opus", "ch000-s03.opus"]
        assert [s["file"] for s in chapter("b1")["segments"]] == ["ch000-s00.opus"]

    def test_a_second_pass_finishes_it(self, make_book, monkeypatch):
        """The point of leaving it pending: the next render fills the one gap under the new map
        and the chapter comes out ready, having re-made a single part.

        Both passes run under the same stub — it only saves the map once, on the very first part
        — because monkeypatch.undo() would take conftest's storage redirect with it."""
        make_book(names=["One"], texts=[self.paragraphs()])
        made = []

        def save_the_map_midway(text, voice, out_path, intro=None, tail_pause=0,
                                respellings=None):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
            made.append((os.path.basename(out_path), respellings))
            if len(made) == 1:
                books.update_book("b1", lambda b: b.update(respell={"Vermeer": "Vermayr"}))

        monkeypatch.setattr(books, "_render_segment", save_the_map_midway)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        books.render_chapter("b1", 0)
        assert chapter("b1")["state"] == "pending"
        assert [f for f, _m in made] == [f"ch000-s{i:02d}.opus" for i in range(4)]

        books.render_chapter("b1", 0)
        assert chapter("b1")["state"] == "ready"
        assert made[4:] == [("ch000-s01.opus", {"Vermeer": "Vermayr"})]   # one part, new map

    def test_an_unchanged_map_is_not_a_straggler(self, make_book, fake_tts):
        make_book(names=["One"], texts=[self.paragraphs()], respell={"Vermeer": "Vermayr"})
        books.render_chapter("b1", 0)
        assert chapter("b1")["state"] == "ready"


class TestAnOpeningNoteSavedMidRender:
    """The other half of the same fault. A render reads the book once and then holds the lock for
    the whole chapter, so a note saved while it runs is missing from what it wrote — and it must
    not then mark the chapter ready saying the old thing."""

    def test_it_is_left_pending_rather_than_ready(self, make_book, monkeypatch):
        make_book(names=["Chapter One"], texts=[LONG], announce=True, opening="First thought.")
        made = []

        def edit_the_note_midway(text, voice, out_path, intro=None, tail_pause=0,
                                 respellings=None):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
            made.append(os.path.basename(out_path))
            if len(made) == 1:                      # while the rest of the chapter is still to go
                books.update_book("b1", lambda b: b.update(opening="Second thought."))

        monkeypatch.setattr(books, "_render_segment", edit_the_note_midway)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        books.render_chapter("b1", 0)

        assert chapter("b1")["state"] == "pending"
        assert not os.path.exists(books.book_dir("b1", "audio", "ch000-s00.opus"))
        assert os.path.exists(books.book_dir("b1", "audio", "ch000-s01.opus"))   # the rest stays

    def test_a_second_pass_says_the_new_note(self, make_book, monkeypatch):
        make_book(names=["Chapter One"], texts=[LONG], announce=True, opening="First thought.")
        made = []

        def edit_the_note_midway(text, voice, out_path, intro=None, tail_pause=0,
                                 respellings=None):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(b"\0" * 64)
            made.append((os.path.basename(out_path), [p for p, _ in (intro or [])]))
            if len(made) == 1:
                books.update_book("b1", lambda b: b.update(opening="Second thought."))

        monkeypatch.setattr(books, "_render_segment", edit_the_note_midway)
        monkeypatch.setattr(books, "audio_seconds", lambda p: 1.0)
        books.render_chapter("b1", 0)
        books.render_chapter("b1", 0)

        assert chapter("b1")["state"] == "ready"
        again = [m for m in made[1:] if m[0] == "ch000-s00.opus"]
        assert len(again) == 1                                  # only the opening was re-made
        assert "Second thought." in again[0][1]
        assert "Second thought." in chapter("b1")["intro"]

    def test_an_untouched_book_still_finishes(self, make_book, fake_tts):
        """The scan runs on every chapter now, not only when the map moved, so the ordinary path
        has to come out ready."""
        make_book(names=["Chapter One"], texts=[LONG], announce=True, opening="A notice.")
        books.render_chapter("b1", 0)
        assert chapter("b1")["state"] == "ready"
