"""The pure functions: reading a chapter number out of a heading, saying it out loud, and
cutting a chapter into renderable pieces. No files, no locks, no engines."""
import pytest

import books


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
        # a number in it and still a title: announcing "7" or "24" would drop what it says
        "Chapter 7: Overcoming Obstacles",
        "AMPOULES REMAINING: 24",
        "Citizen of the Century (2012)",
    ])
    def test_not_numbered(self, label):
        assert books.label_number(label) is None

    def test_digits_win_over_words(self):
        # "Chapter 3" inside a part called "One" must not come out as one
        assert books.label_number("Chapter 3") == 3

    @pytest.mark.parametrize("label,n", [
        ("Chapter I.", 1),          # Pride and Prejudice
        ("CHAPTER II", 2),
        ("IV", 4),
        ("Chapter XLII", 42),
        ("Book IX", 9),
    ])
    def test_roman_numerals(self, label, n):
        """The classics number their chapters this way, and "I" read as a letter is "eye"."""
        assert books.label_number(label) == n

    @pytest.mark.parametrize("label", [
        "MIX",              # a word written in numeral letters — and 1009, so the cap catches it
        "IIII",             # not how four is written
        "DID",
        "I Am Legend",      # a pronoun, and the heading is a title
        "Chapter I. Down the Rabbit-Hole",   # a number and a title: read whole, not as "one"
    ])
    def test_not_a_roman_numeral(self, label):
        assert books.label_number(label) is None


class TestSpokenHeading:
    """A heading read out whole still wants its numeral in digits, or the engine says the
    letters: "chapter eye" for Alice, "chapter eye vee" for chapter four."""

    @pytest.mark.parametrize("written,spoken", [
        ("CHAPTER I. Down the Rabbit-Hole", "CHAPTER 1. Down the Rabbit-Hole"),
        ("Chapter IV. The Rabbit Sends in a Little Bill", "Chapter 4. The Rabbit Sends in a "
                                                          "Little Bill"),
        ("BOOK ONE THE COMING OF THE MARTIANS", "BOOK ONE THE COMING OF THE MARTIANS"),
    ])
    def test_a_numeral_behind_its_word(self, written, spoken):
        assert books.spoken_heading(written) == spoken

    def test_a_numeral_the_heading_opens_with(self):
        """The stop after it is what says it's a number: "II. The Falling Star"."""
        assert books.spoken_heading("II. THE FALLING STAR.") == "2. THE FALLING STAR."

    def test_a_footnote_marker_the_heading_picked_up(self):
        """Max Havelaar's first chapter carries one, and it reads out as a number."""
        assert books.spoken_heading("EERSTE HOOFDSTUK[1]") == "EERSTE HOOFDSTUK"

    @pytest.mark.parametrize("text", [
        "I Am Legend",              # the pronoun, with no word in front marking a number
        "The Vanishing Half",
        "MIX",
    ])
    def test_left_alone(self, text):
        assert books.spoken_heading(text) == text


class TestPartOf:
    def test_splits_on_the_separator(self):
        assert books.part_of("The Night Knocker · Chapter 1") == "The Night Knocker"

    def test_no_part_is_empty(self):
        assert books.part_of("Chapter One") == ""
        assert books.part_of(None) == ""


class TestSpokenTitle:
    """A title is written to be read, not heard. "11/22/63: A Novel" has a subtitle no narrator
    says out loud, so the book can carry its own spoken form — and where it doesn't, the written
    title is already what you'd say."""

    def test_the_written_title_by_default(self):
        assert books.spoken_title({"title": "Dark Matter"}) == "Dark Matter"

    def test_the_spoken_one_wins(self):
        assert books.spoken_title({"title": "11/22/63: A Novel",
                                   "spoken_title": "eleven, twenty-two, sixty-three"}) \
            == "eleven, twenty-two, sixty-three"

    @pytest.mark.parametrize("spoken", ["", "   ", None])
    def test_nothing_in_it_falls_back(self, spoken):
        assert books.spoken_title({"title": "Dark Matter", "spoken_title": spoken}) \
            == "Dark Matter"

    def test_no_title_at_all(self):
        assert books.spoken_title({}) == ""


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
        assert self.said(books.chapter_intro(b, 0)) == ["Dark Matter", "by Blake Crouch", "1"]

    def test_the_spoken_title_is_what_opens_the_book(self):
        b = self.book(["Chapter One"], title="11/22/63: A Novel", author="Stephen King",
                      spoken_title="11, 22, 63")
        assert self.said(books.chapter_intro(b, 0)) \
            == ["11, 22, 63", "by Stephen King", "1"]

    def test_later_chapters_get_only_their_number(self):
        b = self.book(["Chapter One", "Chapter Two"])
        assert self.said(books.chapter_intro(b, 1)) == ["2"]

    def test_the_number_goes_in_as_digits(self):
        """So the engine says it in the language it speaks: espeak reads "19" as "nineteen" for
        an English voice and "negentien" for a Dutch one."""
        b = self.book(["Hoofdstuk 19"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "19"]

    def test_by_is_in_the_books_language(self):
        """Read out by a Dutch voice, the English word comes out as "bie"."""
        b = self.book(["1"], title="Het Juvenalis dilemma", author="Dan Brown", language="nl")
        assert self.said(books.chapter_intro(b, 0)) == [
            "Het Juvenalis dilemma", "van Dan Brown", "1"]

    def test_part_named_only_where_it_begins(self):
        b = self.book(["A · Chapter 1", "A · Chapter 2", "B · Chapter 1"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "A", "1"]
        assert self.said(books.chapter_intro(b, 1)) == ["2"]
        assert self.said(books.chapter_intro(b, 2)) == ["B", "1"]

    def test_the_book_opens_at_the_first_chapter_it_narrates(self):
        """Leaving the publisher's front matter out moves the title and author onto whatever
        comes first now — otherwise the book would never announce itself at all."""
        b = self.book(["Other titles by this author", "Chapter One"], title="T", author="A")
        b["chapters"][0]["skip"] = True
        # the section left out keeps its own heading and nothing else — it's never narrated
        assert self.said(books.chapter_intro(b, 0)) == ["Other titles by this author"]
        assert self.said(books.chapter_intro(b, 1)) == ["T", "by A", "1"]

    def test_a_part_is_named_at_the_first_chapter_of_it_that_is_kept(self):
        b = self.book(["A · Chapter 1", "A · Chapter 2"], title="T", author="")
        b["chapters"][0]["skip"] = True
        assert self.said(books.chapter_intro(b, 1)) == ["T", "A", "2"]

    def test_a_titled_chapter_is_announced_by_its_title(self):
        """Eragon names its chapters instead of numbering them, and the title read as the first
        line of the prose runs straight into the text. Announced, it gets a pause after it."""
        b = self.book(["Prologue: Shade of Fear", "Palancar Valley"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "Prologue: Shade of Fear"]
        assert self.said(books.chapter_intro(b, 1)) == ["Palancar Valley"]

    def test_a_heading_with_both_is_read_whole(self):
        """"Chapter Seven: Overcoming Obstacles" announced as "seven" would lose the title."""
        b = self.book(["Chapter Seven: Overcoming Obstacles", "AMPOULES REMAINING: 24"])
        assert self.said(books.chapter_intro(b, 1)) == ["AMPOULES REMAINING: 24"]
        assert self.said(books.chapter_intro(b, 0))[-1] == "Chapter Seven: Overcoming Obstacles"

    def test_a_section_named_after_its_own_first_words_says_nothing(self):
        """Extraction names a section with no heading and no place in the contents after the
        words it opens with — announcing that would read them out twice."""
        b = self.book(["And Samson called unto the LORD,…", "Chapter One"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T"]      # only the book's own opening
        assert self.said(books.chapter_intro(b, 1)) == ["1"]

    def test_a_section_named_after_its_file_says_nothing(self):
        b = self.book(["fm00.html", "Chapter One"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T"]
        assert self.said(books.chapter_intro(b, 1)) == ["1"]

    def test_a_long_title_is_still_a_title(self):
        """Rich Dad Poor Dad's longest is 74 characters, and it was going unannounced."""
        long = "Chapter Four: Lesson 4: The History of Taxes and the Power of Corporations"
        b = self.book([long], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", long]

    def test_a_title_the_length_of_a_paragraph_is_not_a_title(self):
        b = self.book(["A heading this long is a stray line of prose that came through as one" * 2],
                      title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T"]

    def test_a_contents_that_nests_deeper_than_a_part(self):
        """The Institute's has "Escape · Escape · Chapter 2" — read whole that announces the
        separator out loud and buries the number behind it."""
        b = self.book(["Escape · Escape · Chapter 1", "Escape · Escape · Chapter 2"],
                      title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "Escape", "1"]
        assert self.said(books.chapter_intro(b, 1)) == ["2"]

    def test_a_titled_chapter_inside_a_part(self):
        b = self.book(["A · Shade of Fear", "A · Palancar Valley"], title="T", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["T", "A", "Shade of Fear"]
        assert self.said(books.chapter_intro(b, 1)) == ["Palancar Valley"]

    def test_the_title_is_not_announced_twice(self):
        """A page carrying nothing but the title becomes the part above the first chapter, and
        The Time Machine opened by saying its own name twice over."""
        b = self.book(["The Time Machine · I. Introduction", "II. The Machine"],
                      title="The Time Machine", author="H. G. Wells")
        assert self.said(books.chapter_intro(b, 0)) \
            == ["The Time Machine", "by H G Wells", "1. Introduction"]

    def test_nor_the_start_of_it(self):
        """Such a page is often cut short of the subtitle: Frankenstein's says
        "Frankenstein;"."""
        b = self.book(["Frankenstein;", "Letter 1"],
                      title="Frankenstein; or, the modern prometheus", author="")
        assert self.said(books.chapter_intro(b, 0)) \
            == ["Frankenstein; or, the modern prometheus"]

    def test_a_heading_that_only_shares_a_word_is_kept(self):
        b = self.book(["The Machine"], title="The Time Machine", author="")
        assert self.said(books.chapter_intro(b, 0)) == ["The Time Machine", "The Machine"]

    def test_an_authors_initials_lose_their_stops(self):
        """Each one is read as a letter and then a sentence break, so the name comes out with
        two pauses standing in the middle of it."""
        b = self.book(["Chapter One"], title="A Game of Thrones", author="George R.R. Martin")
        assert self.said(books.chapter_intro(b, 0)) \
            == ["A Game of Thrones", "by George R R Martin", "1"]

    def test_the_opening_note_keeps_its_sentences(self):
        """It's the one part of the announcement that is prose, and a stop ending a sentence
        there is doing its job — "and so did I. Then he left" is not an initial."""
        b = self.book(["Chapter One"], title="T", author="",
                      opening="Nobody knew but I. Then everybody did.")
        assert self.said(books.chapter_intro(b, 0)) \
            == ["T", "Nobody knew but I. Then everybody did.", "1"]

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


class TestOpeningNote:
    """Extraction drops apparatus, and now and then one of those sections is worth hearing — The
    Institute opens with 28 words about missing children that no length rule was going to keep.
    The note is read at the top of the book, after the title and author."""

    def book(self, **extra):
        b = {"id": "b1", "title": "A Book", "author": "An Author", "language": "en",
             "announce": True,
             "chapters": [{"i": 0, "name": "Chapter One"}, {"i": 1, "name": "Chapter Two"}]}
        b.update(extra)
        return b

    def said(self, book, index=0):
        return [p for p, _pause in books.chapter_intro(book, index)]

    def test_it_comes_after_the_author_and_before_the_number(self):
        got = self.said(self.book(opening="A note about something."))
        assert got == ["A Book", "by An Author", "A note about something.", "1"]

    def test_only_at_the_top_of_the_book(self):
        """It's the book's opening, not every chapter's."""
        assert self.said(self.book(opening="A note."), index=1) == ["2"]

    def test_nothing_when_announcements_are_off(self):
        assert self.said(self.book(opening="A note.", announce=False)) == []

    def test_a_book_without_one_is_unchanged(self):
        assert self.said(self.book()) == ["A Book", "by An Author", "1"]
        assert self.said(self.book(opening="   ")) == ["A Book", "by An Author", "1"]

    def test_several_sentences_become_several_pieces(self):
        """One piece per chunk, so a note of a few sentences is a few ordinary calls to the
        engine rather than one long utterance."""
        # Comfortably inside OPENING_CHARS, which has its own test — a note over the cap is
        # trimmed, and the point here is that nothing is dropped by the chunking.
        note = " ".join(f"This is sentence number {i}, which runs on a while." for i in range(15))
        assert len(note) < books.OPENING_CHARS
        got = self.said(self.book(opening=note))
        pieces = got[2:-1]
        assert len(pieces) > 1
        assert " ".join(pieces).split() == note.split()

    def test_the_last_piece_gets_the_longer_pause(self):
        """So the note doesn't run straight into chapter one."""
        pauses = [pause for _p, pause in books.chapter_intro(
            self.book(opening="One sentence here. And a second one here."), 0)]
        assert pauses[-2] == books.OPENING_PAUSE       # the note's last piece
        assert pauses[-1] == books.CHAPTER_PAUSE      # then the chapter number

    def test_it_is_capped(self):
        assert len(books.opening_note({"opening": "x " * 2000})) == books.OPENING_CHARS
