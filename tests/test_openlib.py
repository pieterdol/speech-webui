"""The one thing in the app that talks to another machine.

Nothing here goes near the network: `_get` is replaced with the JSON a real call would have
returned, which is what makes the shaping testable and the suite offline. The blurbs are
invented — a real one is somebody's writing.
"""
import pytest

import openlib


class TestShortening:
    """Their descriptions run to several paragraphs and end in a note about where the blurb
    came from. What's wanted is a reminder of what the book is."""

    def test_a_short_one_is_left_whole(self):
        text = "A surveyor arrives at a village that is not on any map."
        assert openlib.shorten(text) == text

    def test_paragraphs_are_joined(self):
        """The first is often a tagline of half a dozen words, so stopping at it says nothing."""
        text = "One village.\n\nA surveyor arrives, and the village is not on any map."
        assert openlib.shorten(text) == \
            "One village. A surveyor arrives, and the village is not on any map."

    def test_it_stops_at_the_credit(self):
        text = "A surveyor arrives at a village.\n\n-- back cover of the 1971 edition"
        assert openlib.shorten(text) == "A surveyor arrives at a village."

    @pytest.mark.parametrize("credit", ["([source][1])", "[1]: https://example.com/a",
                                        "https://example.com/a", "—the publisher"])
    def test_every_shape_of_credit(self, credit):
        assert openlib.shorten(f"What it is about.\n\n{credit}") == "What it is about."

    def test_links_keep_their_words(self):
        text = "A sequel to [The First One](https://example.com/1), set a winter later."
        assert openlib.shorten(text) == "A sequel to The First One, set a winter later."

    def test_a_long_one_is_cut_at_a_word(self):
        text = "word " * 400
        out = openlib.shorten(text)
        assert len(out) <= openlib.DESCRIPTION_CHARS + 1     # the ellipsis
        assert out.endswith("…") and "wor…" not in out

    def test_nothing_at_all(self):
        assert openlib.shorten("") == ""
        assert openlib.shorten(None) == ""


BLURB = "A surveyor arrives at a village that is on no map, and is not allowed to leave it."


class TestWhatIsSearchedFor:
    """A search that finds nothing is the common failure, so there's more than one to try."""

    def test_the_author_first_then_without_it(self):
        assert openlib.attempts("A Book", "A Writer") == [("A Book", "A Writer"), ("A Book", "")]

    @pytest.mark.parametrize("title,main", [
        ("Moby Dick; Or, The Whale", "Moby Dick"),
        ("Flatland: A Romance of Many Dimensions", "Flatland"),
        ("Max Havelaar / Of de koffiveilingen", "Max Havelaar"),
    ])
    def test_the_subtitle_comes_off_for_a_second_go(self, title, main):
        """An EPUB's metadata title carries it and the catalogue's usually doesn't."""
        assert openlib.attempts(title, "A Writer")[1] == (main, "A Writer")

    def test_a_title_with_no_subtitle_is_not_searched_twice(self):
        assert len(openlib.attempts("A Book", "A Writer")) == 2

    def test_no_author_to_go_on(self):
        assert openlib.attempts("A Book") == [("A Book", "")]

    def test_nothing_at_all(self):
        assert openlib.attempts("   ", "A Writer") == []


class TestDescribe:
    """Two calls per attempt: the search for a work, then the work for its description."""

    def fake(self, monkeypatch, search=None, work=None):
        def _get(url):
            return search if "search.json" in url else work
        monkeypatch.setattr(openlib, "_get", _get)

    def test_a_match_with_a_description(self, monkeypatch):
        self.fake(monkeypatch, search={"docs": [{"key": "/works/OL1W"}]},
                  work={"description": BLURB})
        assert openlib.describe("A Book", "A Writer") == (BLURB, "/works/OL1W")

    def test_a_description_stored_as_a_record(self, monkeypatch):
        """Some carry the text directly and some wrap it in a value."""
        self.fake(monkeypatch, search={"docs": [{"key": "/works/OL1W"}]},
                  work={"description": {"type": "/type/text", "value": BLURB}})
        assert openlib.describe("A Book")[0] == BLURB

    def test_a_work_with_no_description(self, monkeypatch):
        """Normal, especially for a short story: no description and no error."""
        self.fake(monkeypatch, search={"docs": [{"key": "/works/OL1W"}]}, work={})
        assert openlib.describe("A Book", "A Writer") == ("", "")

    def test_a_scrap_is_not_a_description(self, monkeypatch):
        """A duplicate work record often carries a genre tag where the blurb should be."""
        self.fake(monkeypatch, search={"docs": [{"key": "/works/OL1W"}]},
                  work={"description": "Science fiction."})
        assert openlib.describe("A Book") == ("", "")

    def test_nothing_matched(self, monkeypatch):
        self.fake(monkeypatch, search={"docs": []})
        assert openlib.describe("A Book", "A Writer") == ("", "")

    def test_an_edition_is_not_a_work(self, monkeypatch):
        """Only a /works/ key has a description to fetch."""
        self.fake(monkeypatch, search={"docs": [{"key": "/books/OL1M"}]})
        assert openlib.describe("A Book") == ("", "")

    def test_it_moves_on_when_a_match_has_nothing_to_say(self, monkeypatch):
        """A search matching is not the same as a description existing: a public-domain title
        has a dozen work records and most of them are bare."""
        def _get(url):
            if "search.json" in url:
                return {"docs": [{"key": "/works/OL2W" if "author" in url else "/works/OL1W"}]}
            return {"description": BLURB} if "OL1W" in url else {}

        monkeypatch.setattr(openlib, "_get", _get)
        assert openlib.describe("A Book", "A Writer") == (BLURB, "/works/OL1W")

    def test_no_title_asks_nothing(self, monkeypatch):
        monkeypatch.setattr(openlib, "_get",
                            lambda url: pytest.fail("asked with no title to go on"))
        assert openlib.describe("   ") == ("", "")
