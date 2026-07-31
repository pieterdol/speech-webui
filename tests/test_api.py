"""The HTTP surface the page actually talks to. Contracts, not plumbing — the headers a
response carries are as much a part of it as the body."""
import io
import os
import threading

import pytest

import books
import tts


@pytest.fixture(autouse=True)
def no_background_work(monkeypatch):
    """The render endpoints answer immediately and hand the job to a daemon thread. A test
    that let one start would outlive the tmpdir it was writing into, and go on writing after
    pytest had removed it. These tests are about what the endpoint records and returns."""
    started = {"chapters": [], "runs": [], "cast": []}
    monkeypatch.setattr(books, "render_chapter",
                        lambda book_id, i: started["chapters"].append((book_id, i)))
    monkeypatch.setattr(books, "render_all_worker",
                        lambda book_id: started["runs"].append(book_id))
    # Working out who speaks would reach Ollama for minutes. What the endpoint is answerable for is
    # starting it with the right arguments.
    monkeypatch.setattr(books, "cast_worker",
                        lambda jid, book_id, i, model: started["cast"].append((book_id, i, model)))
    return started


class TestBooksIndex:
    def test_empty_library(self, client):
        assert client.get("/api/books").get_json() == {"books": []}

    def test_summary_counts_without_shipping_every_chapter(self, client, make_book):
        make_book(names=["One", "Two", "Three"], texts=["word " * 10] * 3)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        got = client.get("/api/books").get_json()["books"][0]
        assert got["chapters"] == 3 and got["ready"] == 1
        assert isinstance(got["chapters"], int)          # a count, not the list

    def test_unknown_book(self, client):
        assert client.get("/api/books/nope").status_code == 404

    def test_a_book_carries_the_queue_with_it(self, client, make_book):
        make_book()
        body = client.get("/api/books/b1").get_json()
        assert body["book"]["id"] == "b1"
        assert body["narrating"] == {"current": None, "queue": []}


class TestRenderEndpoints:
    def test_render_needs_a_chapter(self, client, make_book):
        make_book()
        r = client.post("/api/books/render", json={"id": "b1"})
        assert r.status_code == 400

    def test_render_unknown_book(self, client):
        r = client.post("/api/books/render", json={"id": "nope", "chapter": 0})
        assert r.status_code == 404

    def test_a_ready_chapter_is_not_restarted(self, client, make_book):
        make_book()
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["started"] == []

    def test_render_all_records_the_part_it_was_asked_for(self, client, make_book,
                                                          no_background_work):
        make_book(names=["A · Chapter 1", "B · Chapter 1"], texts=["word " * 10] * 2)
        client.post("/api/books/render_all", json={"id": "b1", "part": "A"})
        ra = books.find_book("b1")["render_all"]
        assert ra["parts"] == ["A"]
        assert ra["total"] == 1                      # the part's size, not the book's 2
        # The worker takes no scope of its own — it reads the slot, so a part added later is
        # picked up by the one already running.
        assert no_background_work["runs"] == ["b1"]

    def test_a_second_run_does_not_stack(self, client, make_book, no_background_work):
        make_book()
        client.post("/api/books/render_all", json={"id": "b1"})
        assert client.post("/api/books/render_all", json={"id": "b1"}
                           ).get_json().get("already") is True
        assert len(no_background_work["runs"]) == 1

    def test_stop_is_only_a_flag(self, client, make_book):
        make_book(render_all={"running": True, "done": 0, "total": 1})
        client.post("/api/books/render_stop", json={"id": "b1"})
        assert books.find_book("b1")["render_all"]["running"] is False


class TestPosition:
    def test_saved_against_the_book_it_names(self, client, make_book):
        make_book(book_id="playing")
        make_book(book_id="browsing")
        pos = {"chapter": 2, "segment": 1, "offset": 30}
        client.post("/api/books/update", json={"id": "playing", "position": pos})
        assert books.find_book("playing")["position"] == pos
        assert books.find_book("browsing").get("position") is None


class TestSpokenTitle:
    """The opening announcement can be given its own wording without touching the title the
    library and the .m4b are built from. It lives inside chapter 1's first part, so changing it
    means that one file now says the old name."""

    def test_saved_beside_the_written_title(self, client, make_book):
        make_book(title="11/22/63: A Novel")
        client.post("/api/books/update",
                    json={"id": "b1", "spoken_title": "eleven, twenty-two, sixty-three"})
        b = books.find_book("b1")
        assert b["spoken_title"] == "eleven, twenty-two, sixty-three"
        assert b["title"] == "11/22/63: A Novel"

    def test_emptying_it_goes_back_to_the_written_title(self, client, make_book):
        make_book(spoken_title="said differently")
        client.post("/api/books/update", json={"id": "b1", "spoken_title": "   "})
        assert books.spoken_title(books.find_book("b1")) == "A Book"

    def test_a_narrated_opening_is_remade(self, client, make_book, no_background_work):
        make_book(announce=True)                     # or there's no announcement to re-make
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update", json={"id": "b1", "spoken_title": "Said aloud"})
        assert r.get_json()["renamed"] is True
        # pending, but keeping its segments: the render deletes only the part that went stale
        assert books.find_book("b1")["chapters"][0]["state"] == "pending"
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_renaming_the_written_title_counts_too(self, client, make_book, no_background_work):
        """With no spoken form set, the written title is what gets said."""
        make_book(announce=True)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update", json={"id": "b1", "title": "Another Book"})
        assert r.get_json()["renamed"] is True
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_nothing_is_remade_when_nothing_is_announced(self, client, make_book,
                                                        no_background_work):
        """A book with announcements off speaks no title, so renaming it changes no audio. The
        old check compared the title's text and re-recorded the opening regardless."""
        make_book(announce=False)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update", json={"id": "b1", "title": "Another Book"})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []

    def test_but_not_when_the_wording_is_unchanged(self, client, make_book, no_background_work):
        make_book(announce=True, spoken_title="Said aloud")
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update", json={"id": "b1", "spoken_title": "Said aloud"})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []

    def test_nothing_narrated_means_nothing_to_remake(self, client, make_book,
                                                      no_background_work):
        make_book()
        r = client.post("/api/books/update", json={"id": "b1", "spoken_title": "Said aloud"})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []

    def test_a_position_save_never_remakes_anything(self, client, make_book,
                                                    no_background_work):
        """The player writes a position every five seconds. Reading that as a rename would
        re-record the book's opening every five seconds with it."""
        make_book(title="11/22/63: A Novel", spoken_title="eleven, twenty-two, sixty-three")
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update",
                        json={"id": "b1", "position": {"chapter": 3, "segment": 0, "offset": 9}})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []


class TestDelete:
    def test_removes_the_index_entry_and_the_directory(self, client, make_book):
        make_book()
        assert os.path.exists(books.book_dir("b1"))
        assert client.post("/api/books/delete", json={"id": "b1"}).get_json()["ok"]
        assert books.find_book("b1") is None
        assert not os.path.exists(books.book_dir("b1"))

    def test_unknown_book(self, client):
        assert client.post("/api/books/delete", json={"id": "nope"}).status_code == 404


class TestClearNarration:
    def test_keeps_the_book_and_drops_the_audio(self, client, make_book):
        # the state a finished render leaves, built directly — render_chapter is stubbed here,
        # and undoing that would take the tmpdir redirect with it
        make_book(texts=["word " * 400])
        part = books.book_dir("b1", "audio", "ch000-s00.opus")
        os.makedirs(os.path.dirname(part), exist_ok=True)
        open(part, "wb").write(b"\0" * 128)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}], seconds=9.0))
        assert os.path.isdir(books.book_dir("b1", "audio"))
        client.post("/api/books/clear", json={"id": "b1"})
        b = books.find_book("b1")
        assert b is not None
        assert b["chapters"][0]["state"] == "pending"
        assert b["chapters"][0]["segments"] == []
        assert b["position"] == {"chapter": 0, "segment": 0, "offset": 0}
        assert not os.path.isdir(books.book_dir("b1", "audio"))
        assert os.path.exists(books.book_dir("b1", "text", "ch000.txt"))


class TestChapterPreview:
    """One chunk of a chapter, spoken now. The expensive mistakes — the wrong narrator, a name
    the voice mangles, a title that reads badly out loud — all show up in the first minute."""

    LONG = " ".join(["A sentence about nothing much at all."] * 60)

    def test_it_renders_one_chunk_of_the_chapter(self, client, make_book, fake_tts):
        make_book(texts=[self.LONG])
        r = client.get("/api/books/b1/preview/0")
        assert r.status_code == 200 and r.data
        assert len(fake_tts) == 1
        assert fake_tts[0]["text"] == books.split_chunks(self.LONG)[0]
        assert fake_tts[0]["voice"] == "af_heart"

    def test_it_speaks_the_announcement_the_render_would(self, client, make_book, fake_tts):
        """The book's own opening is the thing most worth hearing before eight hours of it."""
        make_book(texts=[self.LONG], announce=True, title="A Book", author="An Author")
        client.get("/api/books/b1/preview/0")
        assert fake_tts[0]["intro"] == books.chapter_intro(books.find_book("b1"), 0)
        assert fake_tts[0]["intro"][0][0] == "A Book"

    def test_the_book_s_pronunciations_reach_it(self, client, make_book, fake_tts):
        make_book(texts=[self.LONG], respell={"sentence": "sentance"})
        client.get("/api/books/b1/preview/0")
        assert fake_tts[0]["respellings"] == {"sentence": "sentance"}

    def test_a_second_tap_replays_rather_than_re_rendering(self, client, make_book, fake_tts):
        make_book(texts=[self.LONG])
        client.get("/api/books/b1/preview/0")
        assert client.get("/api/books/b1/preview/0").status_code == 200
        assert len(fake_tts) == 1

    def test_a_changed_voice_is_a_different_preview(self, client, make_book, fake_tts):
        """Cached under what is actually spoken, so nothing replays a voice you've moved off."""
        make_book(texts=[self.LONG])
        client.get("/api/books/b1/preview/0")
        books.update_book("b1", lambda b: b.update(voice="bm_george"))
        client.get("/api/books/b1/preview/0")
        assert [c["voice"] for c in fake_tts] == ["af_heart", "bm_george"]
        # and the one it replaces goes: it answers a question nobody will ask again
        assert len(os.listdir(books.book_dir("b1", "preview"))) == 1

    def test_nothing_to_read_is_not_a_crash(self, client, make_book, fake_tts):
        make_book(texts=["   "])
        assert client.get("/api/books/b1/preview/0").status_code == 404
        assert fake_tts == []

    def test_a_chapter_that_is_not_there(self, client, make_book, fake_tts):
        make_book()
        assert client.get("/api/books/b1/preview/7").status_code == 404

    def test_unknown_book(self, client):
        assert client.get("/api/books/nope/preview/0").status_code == 404

    def test_an_engine_failure_is_reported_not_cached(self, client, make_book, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("no such voice")
        monkeypatch.setattr(books, "_render_segment", boom)
        make_book(texts=[self.LONG])
        r = client.get("/api/books/b1/preview/0")
        assert r.status_code == 500 and "no such voice" in r.get_json()["error"]

    def test_it_can_be_cached_by_the_browser(self, client, make_book, fake_tts):
        """Named after what it says, so unlike a chapter's parts it can't go stale."""
        make_book(texts=[self.LONG])
        r = client.get("/api/books/b1/preview/0")
        assert "max-age=86400" in r.headers["Cache-Control"]


class TestAutoExportSetting:
    def test_stored_on_the_book_and_turned_off_again(self, client, make_book):
        make_book()
        assert client.post("/api/books/update",
                           json={"id": "b1", "auto_export": True}).get_json()["ok"]
        assert books.find_book("b1")["auto_export"] is True
        client.post("/api/books/update", json={"id": "b1", "auto_export": False})
        assert books.find_book("b1")["auto_export"] is False

    def test_it_changes_nothing_that_was_narrated(self, client, make_book):
        """It decides what happens after a run ends, so unlike the narrator it asks nothing
        and throws nothing away."""
        make_book()
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update", json={"id": "b1", "auto_export": True})
        assert r.status_code == 200 and not r.get_json().get("needs_confirm")
        assert books.find_book("b1")["chapters"][0]["state"] == "ready"


class TestChangingTheNarrator:
    """A voice change invalidates the audio, so it asks first and then discards the lot. What
    counts as audio is the whole question here: a render reuses every part it finds on disk."""

    @pytest.fixture(autouse=True)
    def a_voice_roster(self, monkeypatch):
        monkeypatch.setattr(tts, "kokoro_voices", lambda: ["af_heart", "bm_george"])

    def part(self, book_id="b1", index=0, state="ready"):
        """One chapter with real audio on disk and the index entry to match."""
        name = f"ch{index:03d}-s00.opus"
        p = books.book_dir(book_id, "audio", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 128)
        books.update_book(book_id, lambda b: b["chapters"][index].update(
            state=state, segments=[{"file": name, "seconds": 9.0}], seconds=9.0))
        return p

    def switch(self, client, **extra):
        return client.post("/api/books/update",
                           json={"id": "b1", "voice": "bm_george", **extra})

    def test_nothing_narrated_changes_without_asking(self, client, make_book):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        got = self.switch(client).get_json()
        assert got["ok"] is True and not got.get("needs_confirm")
        assert books.find_book("b1")["voice"] == "bm_george"

    def test_a_narrated_chapter_makes_it_ask(self, client, make_book):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        self.part()
        r = self.switch(client)
        assert r.status_code == 409
        assert r.get_json()["rendered"] == 1
        assert books.find_book("b1")["voice"] == "af_heart"        # unchanged until confirmed

    def test_a_half_narrated_chapter_makes_it_ask_too(self, client, make_book):
        """The bug this class exists for. A stopped render or a restart leaves a chapter with
        parts on disk and a state that isn't ready — real audio in the old voice, which the next
        render of that chapter would reuse and carry on from in the new one."""
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        self.part(state="pending")
        r = self.switch(client)
        assert r.status_code == 409
        assert r.get_json()["rendered"] == 1        # not "0 chapters" about audio it will delete

    def test_confirming_discards_every_part_and_renarrates_where_you_are(
            self, client, make_book, no_background_work):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        self.part(index=0, state="pending")
        self.part(index=1)
        got = self.switch(client, confirm=True).get_json()
        assert got["ok"] is True
        assert books.find_book("b1")["voice"] == "bm_george"
        assert not os.path.isdir(books.book_dir("b1", "audio")) \
            or os.listdir(books.book_dir("b1", "audio")) == []
        assert [c["state"] for c in books.find_book("b1")["chapters"]] == ["pending", "pending"]
        # the one you'd carry on from is re-made now; the rest come back as you reach them
        assert no_background_work["chapters"] == [("b1", 1)]

    def test_the_same_voice_again_is_not_a_change(self, client, make_book):
        make_book(names=["One"], texts=["word " * 10])
        self.part()
        r = client.post("/api/books/update", json={"id": "b1", "voice": "af_heart"})
        assert r.status_code == 200 and not r.get_json().get("needs_confirm")
        assert books.find_book("b1")["chapters"][0]["state"] == "ready"

    def test_turning_the_announcement_off_takes_the_same_route(self, client, make_book):
        """The other setting the audio was made with, and the same rule about half-made parts."""
        make_book(names=["One"], texts=["word " * 10], announce=True)
        self.part(state="pending")
        r = client.post("/api/books/update", json={"id": "b1", "announce": False})
        assert r.status_code == 409


class TestRetryingFailures:
    """A bulk run steps past a chapter that errored, so failures accumulate without anything
    saying so. One button asks for all of them again."""

    def test_only_the_failed_ones_are_asked_for(self, client, make_book, no_background_work):
        make_book(names=["One", "Two", "Three"], texts=["word " * 10] * 3)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        books.update_book("b1", lambda b: b["chapters"][2].update(state="error",
                                                                  error="ffmpeg failed"))
        assert client.post("/api/books/retry", json={"id": "b1"}).get_json()["started"] == [2]
        assert no_background_work["chapters"] == [("b1", 2)]
        states = [c["state"] for c in books.find_book("b1")["chapters"]]
        assert states == ["ready", "pending", "pending"]

    def test_the_reason_is_cleared_and_the_parts_are_kept(self, client, make_book,
                                                          no_background_work):
        """A chapter that fell over on part five has four real parts on disk; the render
        resumes from them, so emptying the list would throw away an hour of audio."""
        make_book(texts=["word " * 10])
        segs = [{"file": "ch000-s00.opus", "seconds": 12.5}]
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="error", error="ffmpeg failed", segments=segs))
        client.post("/api/books/retry", json={"id": "b1"})
        c = books.find_book("b1")["chapters"][0]
        assert c["state"] == "pending" and c["error"] is None
        assert c["segments"] == segs

    def test_a_chapter_left_out_is_not_retried(self, client, make_book, no_background_work):
        """render_chapter returns early on one, so it would sit in the queue for ever."""
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        books.update_book("b1", lambda b: b["chapters"][0].update(state="error", skip=True))
        assert client.post("/api/books/retry", json={"id": "b1"}).get_json()["started"] == []
        assert no_background_work["chapters"] == []
        assert books.find_book("b1")["chapters"][0]["state"] == "error"

    def test_nothing_failed_starts_nothing(self, client, make_book, no_background_work):
        make_book()
        assert client.post("/api/books/retry", json={"id": "b1"}).get_json()["started"] == []
        assert no_background_work["chapters"] == []

    def test_it_says_what_is_already_in_the_engine(self, client, make_book, monkeypatch,
                                                   no_background_work):
        """Renders are serialized across every book, so a retry can be a twenty-minute wait
        with nothing on screen to show it started."""
        make_book()
        books.update_book("b1", lambda b: b["chapters"][0].update(state="error"))
        monkeypatch.setattr(books, "render_depth", lambda: 3)
        assert client.post("/api/books/retry", json={"id": "b1"}).get_json()["ahead"] == 3

    def test_unknown_book(self, client):
        assert client.post("/api/books/retry", json={"id": "nope"}).status_code == 404


class TestLeavingChaptersOut:
    """Apparatus the heuristics can't tell from prose — a publisher's list of their own titles
    is long enough and titled enough to read as a chapter — marked by hand instead."""

    def test_marked_and_unmarked_again(self, client, make_book):
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        assert client.post("/api/books/skip", json={"id": "b1", "chapter": 0}).get_json()["ok"]
        assert books.find_book("b1")["chapters"][0]["skip"] is True
        assert books.find_book("b1")["chapters"][1].get("skip") is not True
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0, "skip": False})
        assert books.find_book("b1")["chapters"][0]["skip"] is False

    def test_the_chapter_keeps_its_number_and_its_text(self, client, make_book):
        """A mark, not a deletion: the text files, the audio files and the saved position are
        all stored under the chapter's index, and renumbering would strand every one of them."""
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0})
        chapters = books.find_book("b1")["chapters"]
        assert [c["i"] for c in chapters] == [0, 1]
        assert os.path.exists(books.book_dir("b1", "text", "ch000.txt"))

    def test_which_chapter_is_required(self, client, make_book):
        make_book()
        assert client.post("/api/books/skip", json={"id": "b1"}).status_code == 400

    def test_a_chapter_that_is_not_there(self, client, make_book):
        make_book()
        assert client.post("/api/books/skip", json={"id": "b1", "chapter": 9}).status_code == 404

    def test_unknown_book(self, client):
        assert client.post("/api/books/skip", json={"id": "nope", "chapter": 0}
                           ).status_code == 404

    def test_the_library_counts_only_what_will_be_narrated(self, client, make_book):
        make_book(names=["Other titles", "One", "Two"], texts=["word " * 10] * 3)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0})
        got = client.get("/api/books").get_json()["books"][0]
        assert got["chapters"] == 2
        assert got["words"] == 20

    def test_it_is_not_narrated_when_asked_for_directly(self, client, make_book,
                                                        no_background_work):
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0})
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["started"] == []
        assert no_background_work["chapters"] == []

    def test_rendering_ahead_reaches_past_it(self, client, make_book, no_background_work):
        """Staying a chapter ahead of the listener means the next chapter they'll hear."""
        make_book(names=["One", "Advertisement", "Two"], texts=["word " * 10] * 3)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 1})
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0, "ahead": True}
                           ).get_json()["started"] == [0, 2]

    def narrated(self, book_id, index):
        books.update_book(book_id, lambda b: b["chapters"][index].update(
            state="ready", seconds=9.0,
            segments=[{"file": f"ch{index:03d}-s00.opus", "seconds": 9.0}]))

    def test_leaving_the_opening_out_moves_the_announcement(self, client, make_book,
                                                            no_background_work):
        """The title and author are spoken at the top of the first chapter that gets narrated.
        The chapter that now opens the book was made without them, so it comes back — with its
        other parts kept, since only the first one has gone stale."""
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        self.narrated("b1", 1)
        assert client.post("/api/books/skip", json={"id": "b1", "chapter": 0}
                           ).get_json()["reopened"] == [1]
        c = books.find_book("b1")["chapters"][1]
        assert c["state"] == "pending" and c["segments"]
        assert no_background_work["chapters"] == [("b1", 1)]

    def test_putting_it_back_moves_the_announcement_off_again(self, client, make_book,
                                                              no_background_work):
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0})
        self.narrated("b1", 1)
        assert client.post("/api/books/skip", json={"id": "b1", "chapter": 0, "skip": False}
                           ).get_json()["reopened"] == [1]
        assert books.find_book("b1")["chapters"][1]["state"] == "pending"

    def test_nothing_is_re_made_when_the_opening_chapter_stays_put(self, client, make_book,
                                                                   no_background_work):
        make_book(names=["One", "Advertisement", "Two"], texts=["word " * 10] * 3)
        self.narrated("b1", 0)
        assert client.post("/api/books/skip", json={"id": "b1", "chapter": 1}
                           ).get_json()["reopened"] == []
        assert books.find_book("b1")["chapters"][0]["state"] == "ready"
        assert no_background_work["chapters"] == []

    def test_a_rescan_that_keeps_the_audio_keeps_the_marks(self, client, make_book,
                                                           monkeypatch):
        """The chapters have to line up exactly for a rescan to keep the narration, and they
        line up for this too — losing the marks would put the apparatus back in."""
        make_book(names=["Other titles", "One"], texts=["word " * 10] * 2)
        client.post("/api/books/skip", json={"id": "b1", "chapter": 0})
        with open(books.book_dir("b1", "book.epub"), "wb") as f:
            f.write(b"not read: extract is stubbed")
        chapters = [{"name": "Other titles", "words": 10, "text": "word " * 10},
                    {"name": "One", "words": 10, "text": "word " * 10}]
        meta = {"title": "A Book", "author": "An Author", "language": "en"}
        monkeypatch.setattr(books.epub, "extract", lambda path: (meta, chapters, []))
        r = client.post("/api/books/rescan", json={"id": "b1"})
        assert r.get_json()["kept_audio"] is True
        assert [c.get("skip") for c in books.find_book("b1")["chapters"]] == [True, False]


class TestAudioHeaders:
    """A part keeps its filename when it's re-rendered, so the response has to let the browser
    find out. Caching it outright played yesterday's audio for a day."""

    def serve(self, client, make_book):
        make_book()
        p = books.book_dir("b1", "audio", "ch000-s00.opus")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 4096)
        return client.get("/book/b1/ch000-s00.opus")

    def test_revalidates(self, client, make_book):
        cc = self.serve(client, make_book).headers["Cache-Control"]
        assert "must-revalidate" in cc
        assert "max-age=0" in cc

    def test_carries_a_validator(self, client, make_book):
        r = self.serve(client, make_book)
        assert r.headers.get("ETag") or r.headers.get("Last-Modified")

    def test_unchanged_answers_304(self, client, make_book):
        r = self.serve(client, make_book)
        again = client.get("/book/b1/ch000-s00.opus",
                           headers={"If-None-Match": r.headers["ETag"]})
        assert again.status_code == 304

    def test_range_requests_still_work(self, client, make_book):
        make_book()
        p = books.book_dir("b1", "audio", "ch000-s00.opus")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 4096)
        r = client.get("/book/b1/ch000-s00.opus", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206

    def test_no_escaping_the_audio_directory(self, client, make_book):
        make_book()
        assert client.get("/book/b1/../../books.json").status_code in (301, 400, 404)


class TestCoverRoute:
    def test_only_the_sizes_that_exist(self, client, make_book):
        """The padded square is gone; asking for it is not a valid size any more."""
        make_book()
        assert client.get("/cover/b1/lock.jpg").status_code == 404

    def test_missing_cover(self, client, make_book):
        make_book()
        assert client.get("/cover/b1/thumb.jpg").status_code == 404

    def test_served_with_a_cache_header(self, client, make_book):
        make_book()
        p = books.cover_path("b1", "thumb")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\xff\xd8\xff\xdb" + b"\0" * 64)     # enough to be sent
        r = client.get("/cover/b1/thumb.jpg")
        assert r.status_code == 200
        assert "max-age" in r.headers["Cache-Control"]


class TestCoverVersion:
    """The cover is cached for a day under a name that never changes, so every response that
    names a book carries the version the page hangs off the URL. Without it a replaced cover
    stays yesterday's image in the library grid."""

    def write_cover(self, mtime=None):
        p = books.cover_path("b1", "thumb")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\xff\xd8\xff\xdb" + b"\0" * 64)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_zero_when_there_is_no_cover(self, client, make_book):
        make_book()
        assert client.get("/api/books").get_json()["books"][0]["cover_v"] == 0

    def test_the_library_listing_carries_it(self, client, make_book):
        make_book()
        self.write_cover(mtime=1_700_000_000)
        assert client.get("/api/books").get_json()["books"][0]["cover_v"] == \
            1_700_000_000_000

    def test_one_book_carries_it_too(self, client, make_book):
        """The reader draws its header from this response, not from the listing."""
        make_book()
        self.write_cover(mtime=1_700_000_000)
        assert client.get("/api/books/b1").get_json()["book"]["cover_v"] == \
            1_700_000_000_000

    def test_a_new_image_is_a_new_version(self, client, make_book, monkeypatch):
        make_book()
        self.write_cover(mtime=1_700_000_000)
        before = client.get("/api/books").get_json()["books"][0]["cover_v"]
        monkeypatch.setattr(books, "make_covers",
                            lambda bid, raw: bool(self.write_cover(mtime=1_700_000_060)))
        r = client.post("/api/books/cover",
                        data={"id": "b1", "file": (io.BytesIO(b"jpeg"), "new.jpg")},
                        content_type="multipart/form-data")
        assert r.get_json()["cover_v"] > before
        assert client.get("/api/books").get_json()["books"][0]["cover_v"] == \
            r.get_json()["cover_v"]


class TestExistingExports:
    """The reader offers every .m4b the book already has, not only the one the current page
    built — the file outlives the panel that linked to it, and re-encoding a 137 MB book to
    get at a copy already on disk is half an hour of GPU for nothing."""

    def write_export(self, name="A Book.m4b", mtime=None, book_id="b1"):
        p = books.book_dir(book_id, "export", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 2048)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_a_book_carries_what_it_has_already_exported(self, client, make_book):
        make_book()
        self.write_export(mtime=1_700_000_000)
        got = client.get("/api/books/b1").get_json()["exports"]
        assert len(got) == 1
        assert got[0]["file"] == "A Book.m4b"
        assert got[0]["bytes"] == 2048
        assert got[0]["made"] == 1_700_000_000

    def test_the_url_is_the_one_that_downloads_it(self, client, make_book):
        """Spaces and all — the name is what the player shows, so it's encoded, not rewritten."""
        make_book()
        self.write_export()
        url = client.get("/api/books/b1").get_json()["exports"][0]["url"]
        assert url == "/export/b1/A%20Book.m4b"
        r = client.get(url)
        assert r.status_code == 200
        assert "attachment" in r.headers["Content-Disposition"]

    def test_nothing_exported_is_an_empty_list(self, client, make_book):
        """Not a 500 from listing a directory the export has never made."""
        make_book()
        assert client.get("/api/books/b1").get_json()["exports"] == []

    def test_newest_first(self, client, make_book):
        make_book()
        self.write_export("Whole.m4b", mtime=1_700_000_000)
        self.write_export("Part One.m4b", mtime=1_700_009_999)
        got = [e["file"] for e in client.get("/api/books/b1").get_json()["exports"]]
        assert got == ["Part One.m4b", "Whole.m4b"]

    def test_only_finished_files_are_offered(self, client, make_book):
        """ffmpeg's own leftovers live in a tmpdir, but a half-written or renamed file in here
        would otherwise be handed over as an audiobook."""
        make_book()
        self.write_export("Done.m4b")
        self.write_export("Done.m4b.part")
        os.makedirs(books.book_dir("b1", "export", "notes"), exist_ok=True)
        got = [e["file"] for e in client.get("/api/books/b1").get_json()["exports"]]
        assert got == ["Done.m4b"]

    def test_one_book_is_not_offered_another_s(self, client, make_book):
        make_book(book_id="mine")
        make_book(book_id="theirs")
        self.write_export("Theirs.m4b", book_id="theirs")
        assert client.get("/api/books/mine").get_json()["exports"] == []

    def test_clearing_the_narration_takes_them(self, client, make_book):
        """The audio they were built from is gone, so an export left behind would be the only
        copy of a narration the book says it hasn't got."""
        make_book()
        self.write_export()
        client.post("/api/books/clear", json={"id": "b1"})
        assert client.get("/api/books/b1").get_json()["exports"] == []


class TestTransferLog:
    """A download the phone abandons and one it keeps are the same 200 in the access log, so
    the export route counts what actually went out. This is the only instrument there is for a
    bug that only happens on the phone."""

    def serve(self, client, headers=None):
        p = books.book_dir("b1", "export", "A Book.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 5000)
        return client.get("/export/b1/A%20Book.m4b", headers=headers or {})

    def test_a_finished_transfer_logs_what_it_sent(self, client, make_book, caplog):
        make_book()
        with caplog.at_level("INFO", logger="speech"):
            assert len(self.serve(client).data) == 5000
        line = "\n".join(caplog.messages)
        assert "A Book.m4b" in line
        assert "sent 5000 of 5000 bytes" in line
        assert "INCOMPLETE" not in line

    def test_the_user_agent_comes_along(self, client, make_book, caplog):
        """Everything arrives from 127.0.0.1 through the tailnet proxy, so the UA is the only
        thing that says whether it was the phone or the PC."""
        make_book()
        with caplog.at_level("INFO", logger="speech"):
            self.serve(client, {"User-Agent": "iPhone/Safari"})
        assert "ua=iPhone/Safari" in "\n".join(caplog.messages)

    def test_a_range_request_is_recorded_as_one(self, client, make_book, caplog):
        make_book()
        with caplog.at_level("INFO", logger="speech"):
            assert self.serve(client, {"Range": "bytes=0-99"}).status_code == 206
        assert "range=bytes=0-99" in "\n".join(caplog.messages)


class TestExportPage:
    """The wrapper page. In the home-screen app iOS opens a target="_blank" link in a browser
    view with no address bar; handed the .m4b itself it renders blank and greys out both Share
    and Open-in-Safari, which is a dead end with a Done button. A page renders, so those
    buttons stay live and Safari can take the download."""

    def make_export(self, name="A Book.m4b"):
        p = books.book_dir("b1", "export", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 3_000_000)
        return p

    def test_it_is_a_page_and_not_the_file(self, client, make_book):
        make_book()
        self.make_export()
        r = client.get("/get/b1/A%20Book.m4b")
        assert r.status_code == 200
        assert r.mimetype == "text/html"
        assert "attachment" not in r.headers.get("Content-Disposition", "")

    def test_it_links_to_the_download(self, client, make_book):
        make_book()
        self.make_export()
        body = client.get("/get/b1/A%20Book.m4b").get_data(as_text=True)
        assert 'href="/export/b1/A%20Book.m4b"' in body
        assert "3.0 MB audiobook" in body        # a decimal, since an EPUB is under a megabyte

    def test_a_name_with_markup_in_it_is_escaped(self, client, make_book):
        """The name comes from the book's title, which the reader can type."""
        make_book()
        self.make_export("A <script>x</script> Book.m4b")
        body = client.get("/get/b1/A%20%3Cscript%3Ex%3C%2Fscript%3E%20Book.m4b"
                          ).get_data(as_text=True)
        assert "<script>x" not in body
        assert "&lt;script&gt;" in body

    def test_nothing_exported_is_a_404(self, client, make_book):
        make_book()
        assert client.get("/get/b1/nothing.m4b").status_code == 404

    def test_no_escaping_the_export_directory(self, client, make_book):
        make_book()
        assert client.get("/get/b1/../../books.json").status_code in (301, 400, 404)


class TestDeletingOneExport:
    """These are the biggest files the app makes — 137 MB for one book — and the only button
    that used to remove them was *Clear narration*, which also deletes every chapter's audio.
    An export can be rebuilt from the narration; the narration can't be rebuilt from anything
    but hours of GPU."""

    def write_export(self, name="A Book.m4b", book_id="b1"):
        p = books.book_dir(book_id, "export", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 1024)
        return p

    def delete(self, client, file, book="b1"):
        return client.post("/api/books/export/delete", json={"id": book, "file": file})

    def test_deletes_the_one_it_was_given(self, client, make_book):
        make_book()
        keep = self.write_export("Part Two.m4b")
        gone = self.write_export("Part One.m4b")
        assert self.delete(client, "Part One.m4b").get_json()["ok"] is True
        assert not os.path.exists(gone)
        assert os.path.exists(keep)

    def test_the_narration_stays(self, client, make_book):
        """The whole point of it being separate from Clear narration."""
        make_book()
        audio = books.book_dir("b1", "audio", "ch000-s00.opus")
        os.makedirs(os.path.dirname(audio), exist_ok=True)
        open(audio, "wb").write(b"\0" * 64)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        self.write_export()
        self.delete(client, "A Book.m4b")
        assert os.path.exists(audio)
        assert books.find_book("b1")["chapters"][0]["state"] == "ready"

    def test_answers_with_what_is_left(self, client, make_book):
        """The page redraws from this rather than from what it assumes the delete did."""
        make_book()
        self.write_export("One.m4b")
        self.write_export("Two.m4b")
        got = self.delete(client, "One.m4b").get_json()["exports"]
        assert [e["file"] for e in got] == ["Two.m4b"]

    def test_unknown_book(self, client):
        assert self.delete(client, "A Book.m4b", book="nope").status_code == 404

    def test_a_file_that_is_not_there(self, client, make_book):
        make_book()
        assert self.delete(client, "never-built.m4b").status_code == 404

    def test_no_climbing_out_of_the_export_directory(self, client, make_book):
        """The filename comes off the wire, so it goes through safe_path like every other one."""
        make_book()
        assert self.delete(client, "../../books.json").status_code == 404
        assert os.path.exists(books.BOOKS_FILE)

    def test_only_exports(self, client, make_book):
        """Inside the right directory but not one of the files this endpoint is about."""
        make_book()
        other = books.book_dir("b1", "export", "notes.txt")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        open(other, "w").write("keep me")
        assert self.delete(client, "notes.txt").status_code == 404
        assert os.path.exists(other)

    def test_one_book_cannot_delete_another_s(self, client, make_book):
        make_book(book_id="mine")
        make_book(book_id="theirs")
        theirs = self.write_export("Theirs.m4b", book_id="theirs")
        assert self.delete(client, "Theirs.m4b", book="mine").status_code == 404
        assert os.path.exists(theirs)


class TestQueueFeedback:
    """A tap on a chapter row is answered by a toast, which has to say which of the two things
    happened — narrating now, or waiting behind something else. The status line it replaced was
    at the foot of the page, several screens below the row, and said neither."""

    def test_nothing_running_is_nothing_ahead(self, client, make_book):
        make_book()
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["ahead"] == 0

    def test_counts_what_is_narrating_and_what_is_waiting(self, client, make_book, monkeypatch):
        """Including another book's chapters: the lock is global, so they're just as much in
        the way as this book's."""
        make_book()
        monkeypatch.setitem(books.render_state, "current", ("other", 7))
        monkeypatch.setattr(books, "render_queue", [("other", 8), ("b1", 3)])
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["ahead"] == 3

    def test_the_tap_is_not_counted_as_ahead_of_itself(self, client, make_book, monkeypatch):
        """Read before the render thread goes. Counting after it would report the first tap on
        an idle machine as one deep, which reads as "queued" when it started immediately."""
        monkeypatch.setattr(books, "render_queue", [])

        def joins_the_queue(book_id, i):
            with books.render_state_lock:
                books.render_queue.append((book_id, i))
            return {"done": threading.Event(), "dropped": False}

        monkeypatch.setattr(books, "queue_render", joins_the_queue)
        make_book()
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["ahead"] == 0


class TestTakingAChapterOffTheQueue:
    """The queue is a list now, so it can be edited. What can't be taken off is the chapter being
    narrated: stopping it part-way through a part leaves a file nothing finishes."""

    def cancel(self, client, chapter, book="b1"):
        return client.post("/api/books/render_cancel", json={"id": book, "chapter": chapter})

    def test_it_comes_off_and_the_queue_says_so(self, client, make_book, monkeypatch):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        monkeypatch.setattr(books, "render_queue", [("b1", 0), ("b1", 1)])
        got = self.cancel(client, 0).get_json()
        assert got["ok"] is True
        assert [e["chapter"] for e in got["narrating"]["queue"]] == [1]
        assert books.render_queue == [("b1", 1)]

    def test_a_chapter_that_is_not_queued(self, client, make_book, monkeypatch):
        """Or whose turn came while the tap was in flight — the same thing to whoever tapped, and
        the queue that comes back with the refusal says which."""
        make_book(names=["One"], texts=["word " * 10])
        monkeypatch.setattr(books, "render_queue", [])
        r = self.cancel(client, 0)
        assert r.status_code == 409
        assert r.get_json()["narrating"]["queue"] == []

    def test_the_one_being_narrated_is_not_on_the_queue(self, client, make_book, monkeypatch):
        make_book(names=["One"], texts=["word " * 10])
        monkeypatch.setattr(books, "render_queue", [])
        monkeypatch.setitem(books.render_state, "current", ("b1", 0))
        assert self.cancel(client, 0).status_code == 409

    def test_unknown_book(self, client, make_book):
        assert self.cancel(client, 0, book="nope").status_code == 404

    def test_which_chapter(self, client, make_book):
        make_book(names=["One"], texts=["word " * 10])
        assert client.post("/api/books/render_cancel",
                           json={"id": "b1"}).status_code == 400


class TestAddingToARun:
    """A book has one run slot, and asking for more while it runs used to be refused. On The
    Institute that meant a run over "Escape" — 22 chapters — left the other 115 with no way to
    be asked for at all: the whole-book button was disabled and another part's button answered
    "already" and did nothing."""

    def parts(self, book_id="b1"):
        return books.run_parts(books.find_book(book_id)["render_all"])

    def start(self, client, part=None):
        return client.post("/api/books/render_all",
                           json={"id": "b1", **({"part": part} if part else {})}).get_json()

    def three_parts(self, make_book):
        names = ([f"A · Chapter {i}" for i in range(2)] + [f"B · Chapter {i}" for i in range(3)]
                 + [f"C · Chapter {i}" for i in range(4)])
        return make_book(names=names, texts=["word " * 10] * 9)

    def test_a_second_part_joins_the_run(self, client, make_book, no_background_work):
        self.three_parts(make_book)
        self.start(client, "A")
        got = self.start(client, "B")
        assert got["added"] == "B"
        assert self.parts() == ["A", "B"]
        assert books.find_book("b1")["render_all"]["total"] == 5     # 2 + 3, not the book's 9

    def test_and_does_not_start_a_second_worker(self, client, make_book, no_background_work):
        """One worker per book: it reads what to do from the slot, so adding to the slot is all
        that's needed. Two workers would race for the same chapters and fight over the counts."""
        self.three_parts(make_book)
        self.start(client, "A")
        self.start(client, "B")
        assert no_background_work["runs"] == ["b1"]

    def test_the_whole_book_widens_a_part_run(self, client, make_book, no_background_work):
        self.three_parts(make_book)
        self.start(client, "A")
        got = self.start(client)
        assert got["widened"] is True
        assert self.parts() == []                                   # [] means everything
        assert books.find_book("b1")["render_all"]["total"] == 9

    def test_a_part_already_in_the_run_changes_nothing(self, client, make_book,
                                                       no_background_work):
        self.three_parts(make_book)
        self.start(client, "A")
        assert self.start(client, "A").get("already") is True
        assert self.parts() == ["A"]

    def test_a_run_over_the_whole_book_already_covers_every_part(self, client, make_book,
                                                                no_background_work):
        self.three_parts(make_book)
        self.start(client)
        assert self.start(client, "B").get("already") is True
        assert self.parts() == []

    def test_the_slot_never_says_two_things_at_once(self, client, make_book, no_background_work):
        """`part` is what a run's scope used to be kept in. Leaving a stale name under the list
        would be a trap for anything still reading it."""
        self.three_parts(make_book)
        self.start(client, "A")
        self.start(client, "B")
        assert books.find_book("b1")["render_all"]["part"] is None

    def test_a_run_left_by_the_old_shape_is_still_read(self, client, make_book,
                                                       no_background_work):
        """A book mid-run when this landed has `part` and no `parts`."""
        self.three_parts(make_book)
        books.update_book("b1", lambda b: b.update(render_all={
            "running": True, "done": 0, "total": 2, "part": "A"}))
        assert self.parts() == ["A"]
        assert self.start(client, "A").get("already") is True
        assert self.start(client, "B")["added"] == "B"
        assert self.parts() == ["A", "B"]


class TestBookRespellings:
    """A book's own pronunciation map, and the audio a change to it re-makes. The map is the
    only way to fix a name an engine says wrong, so saving one has to be cheap: the segments
    that said it the old way go, and nothing else does."""

    # One paragraph per segment: each is just under the 8000-character segment limit, so the
    # word lands in exactly the segment the test means it to.
    FILLER = "Nothing of interest happened here. " * 200
    LONG = "\n".join([FILLER, "Vermeer painted this. " + FILLER, FILLER, FILLER])

    @pytest.fixture(autouse=True)
    def readable_audio(self, monkeypatch):
        """The stand-in parts here are 128 zero bytes, and segments_on_disk deletes anything
        ffprobe can't read a duration out of — right for a part a killed render left half
        written, wrong for a fake one."""
        monkeypatch.setattr(books, "audio_seconds", lambda p: 9.0)

    def post(self, client, mapping, book="b1", **extra):
        return client.post("/api/books/respell",
                           json={"id": book, "respell": mapping, **extra})

    def narrated(self, book_id="b1", index=0):
        """Chapter files on disk plus the index entry a finished render leaves, built by hand —
        render_chapter is stubbed in this module."""
        book = books.find_book(book_id)
        segs = []
        for si, _text in enumerate(books.chapter_segments(book, index)):
            name = f"ch{index:03d}-s{si:02d}.opus"
            p = books.book_dir(book_id, "audio", name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b"\0" * 128)
            segs.append({"file": name, "seconds": 9.0})
        books.update_book(book_id, lambda b: b["chapters"][index].update(
            state="ready", segments=segs, seconds=9.0 * len(segs), total=len(segs), intro=[]))
        return segs

    def test_the_map_is_stored_cleaned(self, client, make_book):
        make_book()
        got = self.post(client, {"  Vermeer ": " Vermayr ", "": "x"}).get_json()
        assert got["ok"] is True
        assert books.find_book("b1")["respell"] == {"Vermeer": "Vermayr"}

    def test_unknown_book(self, client):
        assert self.post(client, {"a": "b"}, book="nope").status_code == 404

    def test_an_identical_map_changes_nothing_at_all(self, client, make_book):
        """Not even `updated` — the page would read that as work having happened."""
        make_book(respell={"Vermeer": "Vermayr"})
        before = books.find_book("b1").get("updated")
        got = self.post(client, {"vermeer": "Vermayr"}).get_json()      # re-cased, same rule
        assert got["unchanged"] is True
        assert books.find_book("b1").get("updated") == before

    def test_it_re_narrates_only_the_parts_that_said_the_word(self, client, make_book,
                                                              no_background_work):
        make_book(names=["One"], texts=[self.LONG])
        segs = self.narrated()
        assert len(segs) == 4                                 # the word is only in the second
        got = self.post(client, {"Vermeer": "Vermayr"}).get_json()

        assert got["chapters"] == [0] and got["parts"] == 1
        assert not os.path.exists(books.book_dir("b1", "audio", "ch000-s01.opus"))
        for keep in ("ch000-s00.opus", "ch000-s02.opus", "ch000-s03.opus"):
            assert os.path.exists(books.book_dir("b1", "audio", keep))
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_the_chapter_keeps_the_parts_that_are_still_right(self, client, make_book,
                                                             no_background_work):
        """Emptying the list would take a playable, current part off the page and out of an
        export. segments_on_disk stops at the gap, which is what the player walks."""
        make_book(names=["One"], texts=[self.LONG])
        self.narrated()
        self.post(client, {"Vermeer": "Vermayr"})              # the second part only
        c = books.find_book("b1")["chapters"][0]
        assert [s["file"] for s in c["segments"]] == ["ch000-s00.opus"]
        assert c["state"] == "pending" and c["seconds"] is None
        assert c["done"] == 1                       # s02 and s03 are on disk but behind the gap

    def test_removing_an_entry_re_narrates_it_too(self, client, make_book, no_background_work):
        make_book(names=["One"], texts=[self.LONG], respell={"Vermeer": "Vermayr"})
        self.narrated()
        got = self.post(client, {}).get_json()
        assert got["removed"] == ["vermeer"]
        assert got["parts"] == 1
        assert books.find_book("b1")["respell"] == {}

    def test_a_word_nothing_says_costs_nothing(self, client, make_book, no_background_work):
        make_book(names=["One"], texts=[self.LONG])
        self.narrated()
        got = self.post(client, {"Rembrandt": "Rembrant"}).get_json()
        assert got["chapters"] == [] and got["parts"] == 0
        assert no_background_work["chapters"] == []
        assert books.find_book("b1")["chapters"][0]["state"] == "ready"

    def test_a_wide_change_asks_first(self, client, make_book, no_background_work):
        """"the" would be correct and catastrophic: it deletes a whole narration."""
        make_book(names=["One", "Two", "Three"], texts=[self.LONG] * 3)
        for i in range(3):
            self.narrated(index=i)
        r = self.post(client, {"Nothing": "Nuthin"})           # in every segment of every chapter
        assert r.status_code == 409
        got = r.get_json()
        assert got["needs_confirm"] is True and got["chapters"] == 3
        assert got["parts"] > 3
        # and it did none of it
        assert books.find_book("b1").get("respell") is None
        assert os.path.exists(books.book_dir("b1", "audio", "ch000-s01.opus"))
        assert no_background_work["chapters"] == []

    def test_and_does_it_when_confirmed(self, client, make_book, no_background_work):
        make_book(names=["One", "Two", "Three"], texts=[self.LONG] * 3)
        for i in range(3):
            self.narrated(index=i)
        got = self.post(client, {"Nothing": "Nuthin"}, confirm=True).get_json()
        assert got["chapters"] == [0, 1, 2]
        assert books.find_book("b1")["respell"] == {"Nothing": "Nuthin"}
        assert sorted(no_background_work["chapters"]) == [("b1", 0), ("b1", 1), ("b1", 2)]

    def test_a_chapter_left_out_is_repaired_but_not_re_narrated(self, client, make_book,
                                                               no_background_work):
        """render_chapter returns early on a skipped chapter, so queueing it would leave a job
        that can never finish — but its stale audio still has to go, or un-skipping it later
        would reveal a chapter that says the old name."""
        make_book(names=["One"], texts=[self.LONG])
        self.narrated()
        books.update_book("b1", lambda b: b["chapters"][0].update(skip=True))
        got = self.post(client, {"Vermeer": "Vermayr"}).get_json()
        assert got["chapters"] == [0]
        assert not os.path.exists(books.book_dir("b1", "audio", "ch000-s01.opus"))
        assert no_background_work["chapters"] == []

    def test_the_listening_position_is_left_where_it_was(self, client, make_book,
                                                        no_background_work):
        """The re-made part has the same name and a length that differs by a fraction of a
        second. Moving the reader would cost more than the drift."""
        make_book(names=["One"], texts=[self.LONG],
                  position={"chapter": 0, "segment": 2, "offset": 44})
        self.narrated()
        self.post(client, {"Vermeer": "Vermayr"})
        assert books.find_book("b1")["position"] == {"chapter": 0, "segment": 2, "offset": 44}

    def test_it_does_not_invalidate_the_whole_book(self, client, make_book, no_background_work):
        """gen means "everything here is wrong" and pairs with deleting the audio directory.
        A pronunciation fix is a handful of files."""
        make_book(names=["One"], texts=[self.LONG])
        self.narrated()
        self.post(client, {"Vermeer": "Vermayr"})
        b = books.find_book("b1")
        assert b["gen"] == 0
        assert b["chapters"][0]["total"] == len(books.chapter_segments(b, 0))
        assert b["chapters"][0]["intro"] == []          # the record of what s00 was made with

    def test_one_book_s_map_is_its_own(self, client, make_book, no_background_work):
        make_book(book_id="mine", names=["One"], texts=[self.LONG])
        make_book(book_id="theirs", names=["One"], texts=[self.LONG])
        self.post(client, {"Vermeer": "Vermayr"}, book="mine")
        assert books.find_book("theirs").get("respell") is None

    def test_a_change_that_re_makes_nothing_does_not_age_the_exports(self, client, make_book,
                                                                    no_background_work):
        """`respell_changed` is what marks an export as saying a word the old way. Adding a
        respelling for a word no narrated audio contains changes no file, so flagging every
        export over it would be crying wolf."""
        make_book(names=["One"], texts=[self.LONG])
        self.narrated()
        p = books.book_dir("b1", "export", "A Book.m4b")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 256)

        self.post(client, {"Rembrandt": "Rembrant"})          # nothing says it
        assert "respell_changed" not in books.find_book("b1")
        assert books.book_exports("b1")[0]["stale"] is False

        self.post(client, {"Vermeer": "Vermayr"})             # this one does
        assert "respell_changed" in books.find_book("b1")
        assert books.book_exports("b1")[0]["stale"] is True


class TestTakingTheEpub:
    """The book itself, to read on the phone. Which is how you find the exact spelling of a name
    the narrator gets wrong — easier to copy off the page than off a voice saying it wrongly.

    It rides the same two routes as an export, under the book's own name rather than book.epub."""

    def store(self, book_id="b1", data=b"PK\x03\x04 epub bytes"):
        p = books.book_dir(book_id, "book.epub")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
        return p

    def test_the_book_offers_it_under_its_own_name(self, client, make_book):
        make_book(title="Dark Matter")
        self.store()
        got = client.get("/api/books/b1").get_json()["book"]["epub"]
        assert got["file"] == "Dark Matter.epub"
        assert got["bytes"] == len(b"PK\x03\x04 epub bytes")
        assert got["url"] == "/export/b1/Dark%20Matter.epub"

    def test_a_book_without_one_offers_nothing(self, client, make_book):
        """Nothing on the page to press, rather than a button that 404s."""
        make_book()
        assert client.get("/api/books/b1").get_json()["book"]["epub"] is None

    def test_a_punctuated_title_still_makes_a_filename(self, client, make_book):
        make_book(title="11/22/63: A Novel")
        self.store()
        assert client.get("/api/books/b1").get_json()["book"]["epub"]["file"] \
            == "112263 A Novel.epub"

    def test_it_downloads_as_the_book_not_as_book_epub(self, client, make_book):
        make_book(title="Dark Matter")
        self.store()
        r = client.get("/export/b1/Dark%20Matter.epub")
        assert r.status_code == 200
        assert r.get_data() == b"PK\x03\x04 epub bytes"
        assert "Dark Matter.epub" in r.headers["Content-Disposition"]
        assert "book.epub" not in r.headers["Content-Disposition"]

    def test_the_wrapper_page_knows_it_is_not_an_audiobook(self, client, make_book):
        make_book(title="Dark Matter")
        self.store()
        body = client.get("/get/b1/Dark%20Matter.epub").get_data(as_text=True)
        assert "EPUB" in body and "audiobook" not in body
        assert 'href="/export/b1/Dark%20Matter.epub"' in body

    def test_the_name_is_the_only_way_to_it(self, client, make_book):
        """Asking for the file on disk by its real name reaches nothing: the resolver only knows
        the book's exports and its EPUB under the title."""
        make_book(title="Dark Matter")
        self.store()
        assert client.get("/export/b1/book.epub").status_code == 404
        assert client.get("/get/b1/book.epub").status_code == 404

    def test_no_climbing_out_with_it(self, client, make_book):
        make_book()
        self.store()
        assert client.get("/export/b1/../../books.json").status_code in (301, 400, 404)
        assert client.get("/export/b1/..%2f..%2fbooks.json").status_code in (301, 400, 404)

    def test_one_book_cannot_ask_for_another_s(self, client, make_book):
        make_book(book_id="mine", title="Mine")
        make_book(book_id="theirs", title="Theirs")
        self.store("theirs")
        assert client.get("/export/mine/Theirs.epub").status_code == 404

    def test_it_is_not_offered_as_an_export(self, client, make_book):
        """The list is audiobooks. The EPUB is a different thing in a different place."""
        make_book(title="Dark Matter")
        self.store()
        assert client.get("/api/books/b1").get_json()["exports"] == []

    def test_and_cannot_be_deleted_through_the_export_endpoint(self, client, make_book):
        make_book(title="Dark Matter")
        p = self.store()
        r = client.post("/api/books/export/delete",
                        json={"id": "b1", "file": "Dark Matter.epub"})
        assert r.status_code == 404
        assert os.path.exists(p)


class TestFindingAWordInTheBook:
    """Typing a respelling needs the written form exactly, which is the one thing a narrator
    saying it wrongly can't tell you. The text is already on disk, so it can be asked."""

    # Invented prose with the shapes that matter: a name, its possessive, a capitalised variant,
    # and a word that merely contains the same letters.
    TEXT = ("Vermeer stood by the window. Vermeer's coat was wet.\n"
            "The gallery on Vermeerkade was shut, and VERMEER was painted over the door.\n"
            "Nobody mentioned Vermeer again.")

    def find(self, client, q, book="b1"):
        return client.get(f"/api/books/{book}/find?q={q}").get_json()

    def test_it_answers_with_the_spellings_that_are_printed(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        got = {f["word"]: f["count"] for f in self.find(client, "verme")["forms"]}
        assert got == {"Vermeer": 3, "Vermeerkade": 1, "VERMEER": 1}

    def test_a_possessive_is_the_word_not_a_form_of_its_own(self, client, make_book):
        """A respelling is keyed on the word and matches the possessive anyway, so offering
        "Vermeer's" as a separate spelling would only be something wrong to type."""
        make_book(names=["One"], texts=[self.TEXT])
        assert "Vermeer's" not in [f["word"] for f in self.find(client, "verme")["forms"]]

    def test_it_says_how_much_of_the_book_says_it(self, client, make_book):
        make_book(names=["One", "Two", "Three"],
                  texts=[self.TEXT, "Vermeer once more.", "Nothing here."])
        top = self.find(client, "Vermeer")["forms"][0]
        assert top["word"] == "Vermeer"
        assert top["count"] == 4 and top["chapters"] == 2

    def test_it_shows_the_word_in_a_phrase(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        line = self.find(client, "Vermeerkade")["forms"][0]["line"]
        assert "Vermeerkade" in line
        assert "\n" not in line                      # collapsed, not a wall of text

    def test_the_commonest_spelling_comes_first(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        assert [f["word"] for f in self.find(client, "verme")["forms"]][0] == "Vermeer"

    def test_case_is_ignored_when_searching(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        assert self.find(client, "VERMEER")["forms"] == self.find(client, "vermeer")["forms"]

    def test_one_letter_is_not_a_search(self, client, make_book):
        """It would match most of the book and answer nothing useful."""
        make_book(names=["One"], texts=[self.TEXT])
        assert self.find(client, "v")["forms"] == []

    def test_a_word_that_is_not_there(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        assert self.find(client, "Rembrandt")["forms"] == []

    def test_a_chapter_with_no_text_file_is_skipped(self, client, make_book):
        make_book(names=["One", "Two"], texts=[self.TEXT, "Vermeer here too."])
        os.remove(books.book_dir("b1", "text", "ch000.txt"))
        assert self.find(client, "Vermeer")["forms"][0]["count"] == 1

    def test_it_answers_with_a_handful_at_most(self, client, make_book):
        make_book(names=["One"], texts=[" ".join(f"word{i}" for i in range(60))])
        assert len(self.find(client, "word")["forms"]) == books.FIND_FORMS

    def test_unknown_book(self, client):
        assert client.get("/api/books/nope/find?q=hello").status_code == 404

    def test_one_book_is_not_searched_for_another(self, client, make_book):
        make_book(book_id="mine", names=["One"], texts=["Nothing of note."])
        make_book(book_id="theirs", names=["One"], texts=[self.TEXT])
        assert self.find(client, "Vermeer", book="mine")["forms"] == []


class TestFindingAPhrase:
    """Several words are looked for as written, through the same pattern a rule uses — so what
    comes back is something you can respell. The Institute opens with a scripture reference that
    carries no full stop, and a narrator reads straight on into the next sentence."""

    TEXT = ("that he slew in his life.\nJudges, Chapter 16\nBut whoso shall offend one of "
            "these little ones.\nA second Judges, Chapter 16 later on.")

    def find(self, client, q):
        return client.get(f"/api/books/b1/find?q={q}").get_json()["forms"]

    def test_a_phrase_is_found_as_written(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        got = self.find(client, "Judges,%20Chapter%2016")
        assert [(f["word"], f["count"]) for f in got] == [("Judges, Chapter 16", 2)]

    def test_a_line_break_inside_it_is_still_the_phrase(self, client, make_book):
        """And it comes back with single spaces, which is what a rule is keyed on."""
        make_book(names=["One"], texts=["a Judges,\nChapter 16 b"])
        assert [f["word"] for f in self.find(client, "Judges,%20Chapter%2016")] \
            == ["Judges, Chapter 16"]

    def test_part_of_a_phrase_answers_the_part(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        assert [f["word"] for f in self.find(client, "Judges,%20Chapter")] == ["Judges, Chapter"]

    def test_a_phrase_that_is_not_there(self, client, make_book):
        make_book(names=["One"], texts=[self.TEXT])
        assert self.find(client, "Kings,%20Chapter%202") == []

    def test_a_single_word_is_still_searched_inside_words(self, client, make_book):
        """The two modes answer different questions: a word you can't spell, or a phrase you
        can. Only the phrase is matched as written."""
        make_book(names=["One"], texts=["Daniela and Danielle"])
        assert sorted(f["word"] for f in self.find(client, "danie")) == ["Daniela", "Danielle"]

    def test_what_it_offers_is_something_a_rule_would_match(self, client, make_book):
        """The point of one shared pattern: a spelling the search hands you must be a key that
        then fires on the same text."""
        import textprep
        make_book(names=["One"], texts=[self.TEXT])
        word = self.find(client, "Judges,%20Chapter%2016")[0]["word"]
        assert textprep.respell(self.TEXT, {word: "SAID"}).count("SAID") == 2


class TestWhatTheIndexKeepsAboutSkippedSections:
    """extract hands over a dropped section's prose so it can be read back, but books.json is an
    index — rewritten after every chapter of every render — and twenty sections of prose in it
    would be paid for on every one of those writes."""

    def test_the_index_keeps_what_it_was_and_why_but_not_the_words(self):
        got = books.skipped_index([{"name": "Dedication", "words": 7, "why": "front matter",
                                    "text": "for someone in particular"}])
        assert got == [{"name": "Dedication", "words": 7, "why": "front matter"}]

    def test_it_is_capped(self):
        many = [{"name": f"s{i}", "words": 1, "why": "x", "text": "y"} for i in range(60)]
        assert len(books.skipped_index(many)) == 40


class TestSettingTheOpeningNote:
    """It lives in the opening announcement, so setting one re-makes exactly the file that
    carries the announcement — the same path a rename takes."""

    def test_a_narrated_opening_is_remade(self, client, make_book, no_background_work):
        make_book(announce=True)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update",
                        json={"id": "b1", "opening": "According to a notice at the front."})
        assert r.get_json()["renamed"] is True
        c = books.find_book("b1")["chapters"][0]
        assert c["state"] == "pending"
        # kept, not cleared: the render deletes only the part the announcement lives in
        assert [s["file"] for s in c["segments"]] == ["ch000-s00.opus"]
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_it_is_stored_on_the_book(self, client, make_book):
        make_book()
        client.post("/api/books/update", json={"id": "b1", "opening": "  A notice.  "})
        assert books.find_book("b1")["opening"] == "A notice."

    def test_clearing_it_also_remakes_the_opening(self, client, make_book, no_background_work):
        make_book(announce=True, opening="A notice.")
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update", json={"id": "b1", "opening": ""})
        assert r.get_json()["renamed"] is True
        assert books.find_book("b1")["opening"] == ""

    def test_setting_the_same_note_again_remakes_nothing(self, client, make_book,
                                                        no_background_work):
        make_book(announce=True, opening="A notice.")
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update", json={"id": "b1", "opening": "A notice."})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []

    def test_it_is_capped(self, client, make_book):
        make_book()
        client.post("/api/books/update", json={"id": "b1", "opening": "x " * 2000})
        assert len(books.find_book("b1")["opening"]) == books.OPENING_CHARS

    def test_a_respelling_of_it_counts_as_a_change(self, client, make_book, no_background_work):
        """The record on the chapter is the spoken form, so how the note is pronounced is part
        of whether the opening on disk is current."""
        make_book(announce=True, opening="About Vermeer.")
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        before = books.chapter_intro(books.find_book("b1"), 0)
        books.update_book("b1", lambda b: b.update(respell={"Vermeer": "Vermayr"}))
        after = books.chapter_intro(books.find_book("b1"), 0)
        assert before == after                                  # the written form is the same…
        b = books.find_book("b1")
        assert [books.respell(p, b["respell"]) for p, _ in after] \
            != [books.respell(p, {}) for p, _ in before]        # …and the spoken one isn't

    def test_an_edit_while_it_is_still_rendering_is_not_lost(self, client, make_book,
                                                            no_background_work):
        """The bug this guard replaced: re-recording used to need the chapter to be *ready* at the
        moment you saved, so a second edit made while the first was still being spoken was stored
        and never said. The render then marked the chapter ready with the older wording, and
        nothing would ever notice."""
        make_book(announce=True)
        books.update_book("b1", lambda b: b["chapters"][0].update(
            state="rendering", segments=[{"file": "ch000-s00.opus", "seconds": 9.0}]))
        r = client.post("/api/books/update", json={"id": "b1", "opening": "A later thought."})
        assert r.get_json()["renamed"] is True
        assert books.find_book("b1")["chapters"][0]["state"] == "pending"
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_but_a_chapter_with_no_audio_yet_needs_no_remake(self, client, make_book,
                                                             no_background_work):
        """Nothing on disk to be wrong — the render will use the note when it gets there."""
        make_book(announce=True)
        r = client.post("/api/books/update", json={"id": "b1", "opening": "A notice."})
        assert r.get_json()["renamed"] is False
        assert no_background_work["chapters"] == []


class TestNarratingAChapterAgain:
    """A render keeps every part it finds on disk, which is what makes resuming an interrupted
    chapter cheap and what makes "do it again" a no-op. When the audio itself is what's wrong — a
    mispronunciation, an announcement fixed after the fact — the only way out was to clear the
    whole book."""

    def narrated(self, parts=3, index=0, state="ready"):
        segs = []
        for si in range(parts):
            name = f"ch{index:03d}-s{si:02d}.opus"
            p = books.book_dir("b1", "audio", name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b"\0" * 64)
            segs.append({"file": name, "seconds": 9.0})
        books.update_book("b1", lambda b: b["chapters"][index].update(
            state=state, segments=segs, seconds=27.0, total=parts))
        return segs

    def test_asking_again_normally_does_nothing(self, client, make_book, no_background_work):
        make_book()
        self.narrated()
        got = client.post("/api/books/render", json={"id": "b1", "chapter": 0}).get_json()
        assert got["started"] == []
        assert len(books.find_book("b1")["chapters"][0]["segments"]) == 3

    def test_redo_throws_the_parts_away_and_starts(self, client, make_book, no_background_work):
        make_book()
        self.narrated()
        got = client.post("/api/books/render",
                          json={"id": "b1", "chapter": 0, "redo": True}).get_json()
        assert got["started"] == [0]
        c = books.find_book("b1")["chapters"][0]
        assert c["state"] == "pending" and c["segments"] == [] and c["seconds"] is None
        assert os.listdir(books.book_dir("b1", "audio")) == []
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_it_only_touches_the_chapter_asked_for(self, client, make_book, no_background_work):
        make_book(names=["One", "Two"], texts=["word " * 10] * 2)
        self.narrated(index=0)
        self.narrated(index=1)
        client.post("/api/books/render", json={"id": "b1", "chapter": 0, "redo": True})
        assert sorted(os.listdir(books.book_dir("b1", "audio"))) \
            == ["ch001-s00.opus", "ch001-s01.opus", "ch001-s02.opus"]
        assert books.find_book("b1")["chapters"][1]["state"] == "ready"

    def test_not_while_it_is_being_narrated(self, client, make_book, no_background_work):
        """The render holds the files it's writing; deleting underneath it would have it publish
        parts that aren't there."""
        make_book()
        self.narrated(state="rendering")
        r = client.post("/api/books/render", json={"id": "b1", "chapter": 0, "redo": True})
        assert r.status_code == 409
        assert len(os.listdir(books.book_dir("b1", "audio"))) == 3
        assert no_background_work["chapters"] == []

    def test_no_such_chapter(self, client, make_book):
        make_book()
        assert client.post("/api/books/render",
                           json={"id": "b1", "chapter": 9, "redo": True}).status_code == 404

    def test_a_chapter_left_out_is_still_left_out(self, client, make_book, no_background_work):
        """redo clears its audio — it asked for that — but a skipped chapter isn't narrated."""
        make_book()
        self.narrated()
        books.update_book("b1", lambda b: b["chapters"][0].update(skip=True))
        got = client.post("/api/books/render",
                          json={"id": "b1", "chapter": 0, "redo": True}).get_json()
        assert got["started"] == []
        assert no_background_work["chapters"] == []


class TestAttributingAChapter:
    """Who speaks which line, over HTTP. The model isn't called here — that's a job on a thread
    and a couple of minutes of GPU; what these are about is what the endpoints record and hand
    back, and what changing a voice throws away."""

    SCENE = "“You are late,” said Marla.\n“The bridge was up.”\n"

    @pytest.fixture(autouse=True)
    def a_voice_roster(self, monkeypatch):
        """Setting a voice checks it exists, which asks the engine, and no test starts one."""
        monkeypatch.setattr(tts, "kokoro_voices",
                            lambda: ["af_heart", "af_bella", "af_nova", "am_adam"])

    def attributed(self, book_id="b1", index=0, speakers=("Marla", "Owen")):
        lines = [{"n": 1, "speaker": speakers[0], "gender": "female", "how": "tag"},
                 {"n": 2, "speaker": speakers[1], "gender": "male", "how": "model"}]
        books.write_attribution(book_id, index, {
            "model": "qwen3:14b", "made": 1, "quotes": 2, "lines": lines, "tagged": 1,
            "speakers": [{"name": speakers[0], "gender": "female", "lines": 1},
                         {"name": speakers[1], "gender": "male", "lines": 1}]})

    def narrated(self, book_id="b1", index=0):
        """A chapter with audio on disk, as a finished render leaves it."""
        name = f"ch{index:03d}-s00.opus"
        p = books.book_dir(book_id, "audio", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\0" * 128)
        books.update_book(book_id, lambda b: b["chapters"][index].update(
            state="ready", segments=[{"file": name, "seconds": 9.0}], seconds=9.0))

    def test_it_answers_with_a_job_to_poll(self, client, make_book, monkeypatch):
        started = []
        monkeypatch.setattr(books.threading, "Thread",
                            lambda target, args, daemon: type("T", (), {
                                "start": lambda self: started.append(args)})())
        make_book(names=["One"], texts=[self.SCENE])
        got = client.post("/api/books/cast", json={"id": "b1", "chapter": 0}).get_json()
        assert got["ok"] is True and got["job_id"]
        assert started[0][1:] == ("b1", 0, "qwen3:14b")

    def test_unknown_book(self, client):
        assert client.post("/api/books/cast",
                           json={"id": "nope", "chapter": 0}).status_code == 404

    def test_no_such_chapter(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE])
        assert client.post("/api/books/cast",
                           json={"id": "b1", "chapter": 9}).status_code == 404

    def test_a_chapter_never_attributed_has_nothing_to_show(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE])
        assert client.get("/api/books/b1/cast/0").status_code == 404

    def test_it_hands_back_the_cast_with_their_voices_and_every_line(self, client, make_book):
        """Every line, because reading them is the only way to see an attribution is wrong
        without listening to an hour of narration."""
        make_book(names=["One"], texts=[self.SCENE], cast={"Marla": "af_bella"})
        self.attributed()
        got = client.get("/api/books/b1/cast/0").get_json()
        assert got["model"] == "qwen3:14b" and got["quotes"] == 2
        assert got["cast"][0] == {"name": "Marla", "gender": "female", "lines": 1,
                                 "voice": "af_bella"}
        assert got["cast"][1]["voice"] == "af_heart"      # no voice of their own: the narrator
        assert [l["speaker"] for l in got["lines"]] == ["Marla", "Owen"]

    def test_a_voice_can_be_changed(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE], cast={"Marla": "af_bella"})
        self.attributed()
        got = client.post("/api/books/cast/voice",
                          json={"id": "b1", "speaker": "Marla", "voice": "am_adam"}).get_json()
        assert got["ok"] is True
        assert books.find_book("b1")["cast"] == {"Marla": "am_adam"}

    def test_handing_a_character_back_to_the_narrator(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE], cast={"Marla": "af_bella"})
        self.attributed()
        client.post("/api/books/cast/voice", json={"id": "b1", "speaker": "Marla", "voice": ""})
        assert books.find_book("b1")["cast"] == {}

    def test_two_characters_cannot_share_a_voice(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE],
                  cast={"Marla": "af_bella", "Owen": "am_adam"})
        self.attributed()
        r = client.post("/api/books/cast/voice",
                        json={"id": "b1", "speaker": "Owen", "voice": "af_bella"})
        assert r.status_code == 409
        assert books.find_book("b1")["cast"]["Owen"] == "am_adam"

    def test_an_unknown_voice_is_refused(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE], cast={"Marla": "af_bella"})
        self.attributed()
        r = client.post("/api/books/cast/voice",
                        json={"id": "b1", "speaker": "Marla", "voice": "af_nobody"})
        assert r.status_code == 400

    def test_changing_a_voice_re_narrates_the_chapters_they_speak_in(self, client, make_book):
        """Their old voice is in that audio, and the point of changing it is not to hear the old
        one again."""
        make_book(names=["One", "Two"], texts=[self.SCENE, "Nobody spoke.\n"],
                  cast={"Marla": "af_bella"})
        self.attributed()
        self.narrated(index=0)
        self.narrated(index=1)
        got = client.post("/api/books/cast/voice",
                          json={"id": "b1", "speaker": "Marla", "voice": "af_nova"}).get_json()
        assert got["reset"] == [0]
        assert books.find_book("b1")["chapters"][0]["state"] == "pending"
        assert books.find_book("b1")["chapters"][1]["state"] == "ready"     # she isn't in it
        assert not os.path.exists(books.book_dir("b1", "audio", "ch000-s00.opus"))
        assert os.path.exists(books.book_dir("b1", "audio", "ch001-s00.opus"))

    def test_a_chapter_with_no_audio_yet_is_not_reset(self, client, make_book):
        make_book(names=["One"], texts=[self.SCENE], cast={"Marla": "af_bella"})
        self.attributed()
        got = client.post("/api/books/cast/voice",
                          json={"id": "b1", "speaker": "Marla", "voice": "af_nova"}).get_json()
        assert got["reset"] == []

    def test_the_count_on_the_row_follows_the_map(self, client, make_book):
        """A row still claiming two voices when one of them has been handed back is the kind of
        stale number nobody thinks to distrust."""
        make_book(names=["One"], texts=[self.SCENE],
                  cast={"Marla": "af_bella", "Owen": "am_adam"})
        self.attributed()
        books.update_book("b1", lambda b: b["chapters"][0].update(cast=2))
        client.post("/api/books/cast/voice", json={"id": "b1", "speaker": "Owen", "voice": ""})
        assert books.find_book("b1")["chapters"][0]["cast"] == 1
