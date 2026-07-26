"""The pure functions: reading a chapter number out of a heading, saying it out loud, and
cutting a chapter into renderable pieces. No files, no locks, no engines."""
import pytest

import books
import core


class TestNumberWord:
    """Numbers are spoken as words so a voice can't read "21" as a year."""

    @pytest.mark.parametrize("n,said", [
        (1, "one"), (9, "nine"), (13, "thirteen"), (20, "twenty"), (21, "twenty-one"),
        (30, "thirty"), (99, "ninety-nine"), (100, "one hundred"), (112, "one hundred twelve"),
        (999, "nine hundred ninety-nine"),
    ])
    def test_spoken(self, n, said):
        assert books.number_word(n) == said

    def test_out_of_range_stays_digits(self):
        assert books.number_word(1000) == "1000"


class TestLabelNumber:
    """A book may write its chapter numbers either way — Dark Matter spells them out, The
    Institute doesn't — and a section that is a title rather than a number gets no number."""

    @pytest.mark.parametrize("label,n", [
        ("Chapter 1", 1),
        ("Chapter One", 1),
        ("Chapter Two", 2),
        ("Chapter Eleven", 11),
        ("Chapter Twenty-One", 21),
        ("Chapter Twenty One", 21),
        ("Chapter Ninety-Nine", 99),
        ("One Hundred Twelve", 112),
        ("Nine", 9),
        ("24", 24),
    ])
    def test_numbered(self, label, n):
        assert books.label_number(label) == n

    @pytest.mark.parametrize("label", [
        "The Night Knocker",
        "And Samson called unto the LORD,…",
        "Epilogue",
        "Appendix",
        "",
        None,
    ])
    def test_not_numbered(self, label):
        assert books.label_number(label) is None

    def test_digits_win_over_words(self):
        # "Chapter 3" inside a part called "One" must not come out as one
        assert books.label_number("Chapter 3") == 3


class TestPartOf:
    def test_splits_on_the_separator(self):
        assert books.part_of("The Night Knocker · Chapter 1") == "The Night Knocker"

    def test_no_part_is_empty(self):
        assert books.part_of("Chapter One") == ""
        assert books.part_of(None) == ""


class TestChapterIntro:
    """What gets spoken before the prose."""

    def book(self, names, **kw):
        b = {"title": "Dark Matter", "author": "Blake Crouch", "announce": True,
             "chapters": [{"i": i, "name": n} for i, n in enumerate(names)]}
        b.update(kw)
        return b

    def said(self, intro):
        return [phrase for phrase, _pause in intro]

    def test_book_opens_with_title_and_author(self):
        b = self.book(["Chapter One", "Chapter Two"])
        assert self.said(books.chapter_intro(b, 0)) == ["Dark Matter", "by Blake Crouch", "one"]

    def test_later_chapters_get_only_their_number(self):
        b = self.book(["Chapter One", "Chapter Two"])
        assert self.said(books.chapter_intro(b, 1)) == ["two"]

    def test_part_named_only_where_it_begins(self):
        b = self.book(["A · Chapter 1", "A · Chapter 2", "B · Chapter 1"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "A", "one"]
        assert self.said(books.chapter_intro(b, 1)) == ["two"]
        assert self.said(books.chapter_intro(b, 2)) == ["B", "one"]

    def test_unnumbered_section_says_nothing_of_its_own(self):
        b = self.book(["An epigraph", "Chapter One"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T"]      # only the book's own opening
        assert self.said(books.chapter_intro(b, 1)) == ["one"]

    def test_announcements_off(self):
        b = self.book(["Chapter One"], announce=False)
        assert books.chapter_intro(b, 0) == []

    def test_out_of_range(self):
        assert books.chapter_intro(self.book(["Chapter One"]), 7) == []

    def test_pauses_are_real_seconds(self):
        b = self.book(["A · Chapter 1"], title="T", author="")
        pauses = [p for _phrase, p in books.chapter_intro(b, 0)]
        assert all(p > 0 for p in pauses)


class TestSplitting:
    def test_segments_stay_under_the_limit(self):
        text = "\n".join(["word " * 100] * 40)          # ~20k chars
        segs = books.split_segments(text, limit=2000)
        assert len(segs) > 1
        assert all(len(s) <= 2000 * 1.5 for s in segs)   # paragraph-granular, so allow slack

    def test_no_text_is_lost(self):
        text = "\n".join(f"Paragraph {i} has some words in it." for i in range(60))
        joined = " ".join(books.split_segments(text, limit=200)).split()
        assert joined == text.split()

    def test_a_single_huge_paragraph_is_cut_at_sentences(self):
        para = " ".join(f"Sentence number {i}." for i in range(400))
        segs = books.split_segments(para, limit=500)
        assert len(segs) > 1
        assert " ".join(segs).split() == para.split()

    def test_empty_text(self):
        assert books.split_segments("") == []
        assert books.split_segments("\n\n  \n") == []

    def test_chunks_are_whole_sentences(self):
        text = " ".join(f"This is sentence {i}." for i in range(80))
        chunks = books.split_chunks(text, limit=200)
        assert len(chunks) > 1
        for c in chunks:
            assert c.endswith(".")
        assert " ".join(chunks).split() == text.split()


class TestScoping:
    """chapters_in and book_parts are what "narrate this part" and "export this part" mean."""

    def book(self):
        names = ["An epigraph", "A · Chapter 1", "A · Chapter 2", "B · Chapter 1"]
        return {"chapters": [{"i": i, "name": n, "words": 10,
                              "state": "ready" if i == 1 else "pending"}
                             for i, n in enumerate(names)]}

    def test_no_part_means_the_whole_book(self):
        assert len(books.chapters_in(self.book(), None)) == 4

    def test_a_part_is_only_its_own_chapters(self):
        got = books.chapters_in(self.book(), "A")
        assert [c["i"] for c in got] == [1, 2]

    def test_parts_report_their_own_progress(self):
        parts = {p["part"]: p for p in books.book_parts(self.book())}
        assert parts["A"]["chapters"] == 2 and parts["A"]["ready"] == 1
        assert parts["B"]["chapters"] == 1 and parts["B"]["ready"] == 0
        assert parts[""]["chapters"] == 1        # the standalone section
