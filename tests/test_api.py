"""The HTTP surface the page actually talks to. Contracts, not plumbing — the headers a
response carries are as much a part of it as the body."""
import io
import os

import pytest

import books


@pytest.fixture(autouse=True)
def no_background_work(monkeypatch):
    """The render endpoints answer immediately and hand the job to a daemon thread. A test
    that let one start would outlive the tmpdir it was writing into, and go on writing after
    pytest had removed it. These tests are about what the endpoint records and returns."""
    started = {"chapters": [], "runs": []}
    monkeypatch.setattr(books, "render_chapter",
                        lambda book_id, i: started["chapters"].append((book_id, i)))
    monkeypatch.setattr(books, "render_all_worker",
                        lambda book_id: started["runs"].append(book_id))
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
        make_book()
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update", json={"id": "b1", "spoken_title": "Said aloud"})
        assert r.get_json()["renamed"] is True
        # pending, but keeping its segments: the render deletes only the part that went stale
        assert books.find_book("b1")["chapters"][0]["state"] == "pending"
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_renaming_the_written_title_counts_too(self, client, make_book, no_background_work):
        """With no spoken form set, the written title is what gets said."""
        make_book()
        books.update_book("b1", lambda b: b["chapters"][0].update(state="ready"))
        r = client.post("/api/books/update", json={"id": "b1", "title": "Another Book"})
        assert r.get_json()["renamed"] is True
        assert no_background_work["chapters"] == [("b1", 0)]

    def test_but_not_when_the_wording_is_unchanged(self, client, make_book, no_background_work):
        make_book(spoken_title="Said aloud")
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
        assert "3 MB" in body

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
        monkeypatch.setitem(books.render_state, "waiting", [("other", 8), ("b1", 3)])
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["ahead"] == 3

    def test_the_tap_is_not_counted_as_ahead_of_itself(self, client, make_book, monkeypatch):
        """Read before the render thread goes. Counting after it would report the first tap on
        an idle machine as one deep, which reads as "queued" when it started immediately."""
        monkeypatch.setitem(books.render_state, "waiting", [])

        def joins_the_queue(book_id, i):
            with books.render_state_lock:
                books.render_state["waiting"].append((book_id, i))

        monkeypatch.setattr(books, "render_chapter", joins_the_queue)
        make_book()
        assert client.post("/api/books/render", json={"id": "b1", "chapter": 0}
                           ).get_json()["ahead"] == 0


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
