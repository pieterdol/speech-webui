"""EPUB extraction, against EPUBs built here rather than real books.

Real books are gitignored — they're copyrighted and large — so the fixtures are the smallest
files that still exercise what matters: the TOC nesting that turns into "Part · Chapter", the
front matter that gets dropped, and a flat TOC that legitimately has no parts to find.
"""
import os
import shutil
import zipfile

import pytest

import books
import epub

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def page(title, body_words=400, heading=None):
    head = f"<h2>{heading}</h2>" if heading else ""
    words = " ".join(f"word{i}" for i in range(body_words))
    return f"<html><head><title>{title}</title></head><body>{head}<p>{words}</p></body></html>"


def build(tmp_path, docs, navpoints, name="t.epub"):
    """docs: [(filename, html)]. navpoints: nested [(label, href, [children])]."""
    manifest = "".join(
        f'<item id="i{n}" href="{f}" media-type="application/xhtml+xml"/>'
        for n, (f, _h) in enumerate(docs))
    spine = "".join(f'<itemref idref="i{n}"/>' for n in range(len(docs)))
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title><dc:creator>A Writer</dc:creator><dc:language>en</dc:language>
  </metadata>
  <manifest>{manifest}<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
  <spine toc="ncx">{spine}</spine>
</package>"""

    counter = [0]

    def points(items):
        out = ""
        for label, href, kids in items:
            counter[0] += 1
            out += (f'<navPoint id="n{counter[0]}" playOrder="{counter[0]}">'
                    f'<navLabel><text>{label}</text></navLabel>'
                    f'<content src="{href}"/>{points(kids)}</navPoint>')
        return out

    ncx = f"""<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>{points(navpoints)}</navMap>
</ncx>"""

    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        for f, h in docs:
            z.writestr(f, h)
    return str(path)


class TestNestedToc:
    """A TOC that puts chapters under parts is where "The Night Knocker · Chapter 1" comes
    from — the part prefix is joined on with the separator the rest of the app splits on."""

    @pytest.fixture
    def book(self, tmp_path):
        docs = [(f"c{i}.html", page(f"c{i}")) for i in range(4)]
        nav = [("Part One", "c0.html", [("Chapter 1", "c0.html", []),
                                        ("Chapter 2", "c1.html", [])]),
               ("Part Two", "c2.html", [("Chapter 1", "c2.html", []),
                                        ("Chapter 2", "c3.html", [])])]
        return build(tmp_path, docs, nav)

    def test_names_carry_their_part(self, book):
        _meta, chapters, _skipped = epub.extract(book)
        assert [c["name"] for c in chapters] == [
            "Part One · Chapter 1", "Part One · Chapter 2",
            "Part Two · Chapter 1", "Part Two · Chapter 2"]

    def test_the_separator_is_the_one_the_app_splits_on(self, book):
        _meta, chapters, _skipped = epub.extract(book)
        assert books.part_of(chapters[0]["name"]) == "Part One"
        assert books.label_number(chapters[3]["name"].split(books.PART_SEP, 1)[1]) == 2

    def test_metadata(self, book):
        meta, _chapters, _skipped = epub.extract(book)
        assert meta["title"] == "Test Book"
        assert meta["author"] == "A Writer"


class TestSeveralChaptersPerFile:
    """A book can pack twenty chapters into one spine document and address each from the TOC by
    fragment. Keying names by file alone made that one chapter of 20,000 words under the last
    label in the file."""

    def packed(self, tmp_path, chapters):
        """One document holding several anchored chapters, the way calibre exports them."""
        body = "".join(
            f'<h4 id="heading_id_{n}">{label}</h4><p>{" ".join(f"word{i}" for i in range(words))}</p>'
            for n, (label, words) in enumerate(chapters, start=2))
        docs = [("packed.html", f"<html><body>{body}</body></html>")]
        nav = [(chapters[0][0], "packed.html", [])] + [
            (label, f"packed.html#heading_id_{n}", [])
            for n, (label, _w) in enumerate(chapters[1:], start=3)]
        return build(tmp_path, docs, [(l, h, k) for l, h, k in nav])

    def test_each_anchor_becomes_its_own_chapter(self, tmp_path):
        book = self.packed(tmp_path, [("PROLOOG", 200), ("1", 300), ("2", 400)])
        _meta, chapters, _skipped = epub.extract(book)
        assert [c["name"] for c in chapters] == ["PROLOOG", "1", "2"]
        assert [c["words"] for c in chapters] == [200, 300, 400]

    def test_the_text_is_partitioned_not_copied(self, tmp_path):
        """Every word lands in exactly one chapter — the file's own total."""
        book = self.packed(tmp_path, [("PROLOOG", 200), ("1", 300), ("2", 400)])
        _meta, chapters, _skipped = epub.extract(book)
        assert sum(c["words"] for c in chapters) == 900
        assert "word299" in chapters[1]["text"] and "word299" not in chapters[0]["text"]

    def test_a_short_chapter_the_toc_names_is_kept(self, tmp_path):
        """The length rule drops part-title pages and stray lines, which is a question about
        untitled sections. Some real chapters are 90 words."""
        book = self.packed(tmp_path, [("PROLOOG", 200), ("1", 90), ("2", 300)])
        _meta, chapters, _skipped = epub.extract(book)
        assert [c["name"] for c in chapters] == ["PROLOOG", "1", "2"]

    def test_a_missing_anchor_does_not_lose_the_text(self, tmp_path):
        """A TOC pointing at an id the document doesn't have makes no cut, so its text stays
        with the chapter before it rather than vanishing."""
        docs = [("packed.html",
                 "<html><body><h4 id=\"heading_id_2\">1</h4><p>one two three</p>"
                 "<h4>2</h4><p>four five six</p></body></html>")]
        book = build(tmp_path, docs, [("1", "packed.html", []),
                                     ("2", "packed.html#nope", [])])
        _meta, chapters, _skipped = epub.extract(book)
        assert len(chapters) == 1
        assert "four five six" in chapters[0]["text"]


class TestATitleOnAPageOfItsOwn:
    """The Gunslinger's shape: a page carrying nothing but the chapter's title, which the
    contents names, and then the chapter itself on the page after it, which it doesn't. It was
    listing every chapter twice — "Chapter 1: The Gunslinger" with two words in it, then
    "CHAPTER 1" with the story."""

    def make(self, tmp_path, title_page="THE OUTSET", label="Chapter 1: The Outset"):
        # The contents points at the title page by anchor, which is what keeps a page of two
        # words from being dropped as a stray line.
        docs = [("t1.html", f'<html><body><h1 id="top">{title_page}</h1></body></html>'),
                ("c1.html", page("body", 400, heading="CHAPTER 1"))]
        return build(tmp_path, docs, [(label, "t1.html#top", [])])

    def test_the_two_become_one_chapter(self, tmp_path):
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        assert [c["name"] for c in chapters] == ["Chapter 1: The Outset"]

    def test_it_keeps_the_name_from_the_contents(self, tmp_path):
        """Which is the better of the two: "CHAPTER 1" is only what the page happens to say."""
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        assert chapters[0]["words"] > 400            # the story, not the two-word page
        assert "word399" in chapters[0]["text"]

    def test_both_headings_then_come_off_the_text(self, tmp_path):
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        c = chapters[0]
        assert epub.strip_heading(c["text"], c["name"]).startswith("word0")

    def test_a_short_section_that_is_not_a_title_stays_its_own_chapter(self, tmp_path):
        """A stray quotation before a chapter is short too, and it isn't its heading."""
        docs = [("ep.html", '<html><body><p id="top">Every road runs out somewhere, said the '
                            'sign at the edge of the last town.</p></body></html>'),
                ("c1.html", page("body", 400, heading="CHAPTER 1"))]
        book = build(tmp_path, docs, [("Before the Road", "ep.html#top", [])])
        _meta, chapters, _skipped = epub.extract(book)
        assert [c["name"] for c in chapters] == ["Before the Road", "CHAPTER 1"]


class TestAPartTitleOverTheFirstChapter:
    """The War of the Worlds' shape: one document holding "BOOK ONE THE COMING OF THE MARTIANS"
    and then "I. THE EVE OF THE WAR.", with only the part in the contents. The chapter went in
    under the part's name, and its own heading stayed in the prose to be read as "eye"."""

    def make(self, tmp_path, second="I. THE EVE OF THE WAR."):
        doc = ("<html><body><h1>BOOK ONE THE COMING OF THE MARTIANS</h1>"
               f"<h2>{second}</h2><p>{' '.join(f'word{i}' for i in range(400))}</p></body></html>")
        return build(tmp_path, [("b1.html", doc)],
                     [("BOOK ONE THE COMING OF THE MARTIANS", "b1.html", [])])

    def test_the_chapters_own_heading_joins_the_name(self, tmp_path):
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        assert [c["name"] for c in chapters] == [
            "BOOK ONE THE COMING OF THE MARTIANS · I. THE EVE OF THE WAR."]

    def test_the_part_and_the_chapter_are_then_both_announceable(self, tmp_path):
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        name = chapters[0]["name"]
        assert books.part_of(name) == "BOOK ONE THE COMING OF THE MARTIANS"
        assert books.spoken_heading(books.heading_of(name)) == "1. THE EVE OF THE WAR."

    def test_both_heading_lines_come_off_the_prose(self, tmp_path):
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path))
        c = chapters[0]
        assert epub.strip_heading(c["text"], c["name"]).startswith("word0")

    def test_a_second_line_that_is_prose_is_left_alone(self, tmp_path):
        """The line under the part title is the chapter only when it opens with a number."""
        _meta, chapters, _skipped = epub.extract(self.make(tmp_path, second="It began quietly."))
        assert chapters[0]["name"] == "BOOK ONE THE COMING OF THE MARTIANS"


class TestFlatToc:
    """1984's shape: three entries, no children. There are no parts to find, and saying so is
    correct rather than a failure."""

    def test_no_part_prefixes(self, tmp_path):
        docs = [(f"c{i}.html", page(f"c{i}")) for i in range(3)]
        nav = [("PART I", "c0.html", []), ("PART II", "c1.html", []),
               ("PART III", "c2.html", [])]
        _meta, chapters, _skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["PART I", "PART II", "PART III"]
        assert all(books.part_of(c["name"]) == "" for c in chapters)


class TestSkipping:
    def test_front_and_back_matter_dropped(self, tmp_path):
        docs = [("cover.html", page("Cover", 2)),
                ("copy.html", page("Copyright", 30)),
                ("c1.html", page("Chapter 1", 400)),
                ("toc.html", page("Contents", 20))]
        nav = [("Cover", "cover.html", []), ("Copyright", "copy.html", []),
               ("Chapter 1", "c1.html", []), ("Contents", "toc.html", [])]
        _meta, chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1"]
        assert {s["name"] for s in skipped} >= {"Cover", "Copyright", "Contents"}
        assert all("why" in s for s in skipped)

    def test_a_gutenberg_text_loses_its_wrapper(self, tmp_path):
        """Every Project Gutenberg book opens with a header page and closes with the 343-word
        licence, both long enough to be chapters and neither one the book."""
        docs = [("hdr.html", page("The Project Gutenberg eBook of Something", 130)),
                ("c1.html", page("Chapter 1", 400)),
                ("lic.html", page("THE FULL PROJECT GUTENBERG™ LICENSE", 343))]
        nav = [("The Project Gutenberg eBook of Something", "hdr.html", []),
               ("Chapter 1", "c1.html", []),
               ("THE FULL PROJECT GUTENBERG™ LICENSE", "lic.html", [])]
        _meta, chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1"]
        assert all(s["why"] == "looks like front or back matter" for s in skipped)

    def test_short_sections_dropped_with_a_reason(self, tmp_path):
        docs = [("a.html", page("Tiny", 5)), ("b.html", page("Chapter 1", 400))]
        nav = [("Tiny", "a.html", []), ("Chapter 1", "b.html", [])]
        _meta, chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1"]
        assert "words" in next(s["why"] for s in skipped if s["name"] == "Tiny")

    def test_a_dropped_section_says_where_it_would_have_gone(self, tmp_path):
        """What lets one be put back as a chapter where the book has it rather than only at the
        top: the chapters kept so far are exactly the position it would have held."""
        docs = [("c1.html", page("Chapter 1", 400)), ("note.html", page("Notice", 28)),
                ("c2.html", page("Chapter 2", 400))]
        nav = [("Chapter 1", "c1.html", []), ("Notice", "note.html", []),
               ("Chapter 2", "c2.html", [])]
        _meta, chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1", "Chapter 2"]
        assert next(s["at"] for s in skipped if s["name"] == "Notice") == 1

    def test_a_dropped_section_keeps_its_text(self, tmp_path):
        """Dropped from the narration, not thrown away: a dedication or a notice can be read
        back at the top of the book, and its words are the only place to get it from."""
        docs = [("ded.html", page("Dedication", 7)), ("b.html", page("Chapter 1", 400))]
        nav = [("Dedication", "ded.html", []), ("Chapter 1", "b.html", [])]
        _meta, _chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        got = next(s for s in skipped if s["name"] == "Dedication")
        assert got["text"].split() == [f"word{i}" for i in range(7)]
        assert got["words"] == 7


class TestChapterBodies:
    def test_each_chapter_carries_its_name_words_and_text(self, tmp_path):
        """extract returns name/words/text in reading order; the index is added by the caller
        when it builds the book record."""
        docs = [(f"c{i}.html", page(f"c{i}", 300)) for i in range(3)]
        nav = [(f"Chapter {i+1}", f"c{i}.html", []) for i in range(3)]
        _meta, chapters, _skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
        for c in chapters:
            assert set(c) == {"name", "words", "text"}
            assert c["words"] > 100
            assert "word0" in c["text"]

    def test_word_counts_match_the_text(self, tmp_path):
        docs = [("c0.html", page("Chapter 1", 250))]
        nav = [("Chapter 1", "c0.html", [])]
        _meta, chapters, _skipped = epub.extract(build(tmp_path, docs, nav))
        assert chapters[0]["words"] == len(chapters[0]["text"].split())


class TestStripHeading:
    """The chapter's own heading line comes out of the text, because the spoken lead-in says
    it better than the prose does."""

    def test_removes_a_repeated_title(self):
        assert epub.strip_heading("Chapter 1\nIt was a bright cold day.",
                                  "Chapter 1").startswith("It was")

    def test_the_page_may_shout_what_the_contents_dont(self):
        """Eragon's contents say "Prologue: Shade of Fear"; the page has it in capitals."""
        assert epub.strip_heading("PROLOGUE: SHADE OF FEAR\nA wind blew off the mountain.",
                                  "Prologue: Shade of Fear").startswith("A wind")

    def test_a_heading_set_as_two_blocks_comes_off_whole(self):
        """Rich Dad Poor Dad's pages have "Chapter One" over "LESSON 1: THE RICH DON'T WORK FOR
        MONEY", which the contents joins into one entry."""
        text = ("Chapter One\nLESSON 1: THE RICH DON’T WORK FOR MONEY\n"
                "The poor work for money, which is the whole lesson.")
        assert epub.strip_heading(text, "Chapter One: Lesson 1: The Rich Don’t Work for Money") \
            == "The poor work for money, which is the whole lesson."

    def test_a_numbered_section_under_the_heading_stays(self):
        """11/22/63 numbers the sections inside a chapter. The "1" below "CHAPTER 1" belongs to
        the prose, and taking it off would leave the first section of every chapter the only one
        without its number."""
        text = "CHAPTER 1\n1\nThe first section starts here."
        assert epub.strip_heading(text, "Chapter 1") == "1\nThe first section starts here."

    def test_prose_that_opens_with_the_title_stays(self):
        """Containment runs one way. A line the heading contains is the heading; a line that
        contains the heading is prose using the title's own words."""
        text = "The long walk home began at dawn and ended in the dark."
        assert epub.strip_heading(text, "The Long Walk Home") == text

    def test_a_section_that_is_only_a_heading_keeps_it(self):
        """A part-title page kept as a chapter has nothing else in it, and emptying it would
        leave a chapter with no segments — so no announcement either."""
        assert epub.strip_heading("THE GUNSLINGER", "Chapter 1: The Gunslinger") \
            == "THE GUNSLINGER"

    def test_the_books_title_and_author_come_off_too(self):
        """A half-title page prints both above the first chapter, and the lead-in has just
        announced them: The War of the Worlds said "by H. G. Wells" twice over."""
        text = ("The War of the Worlds\nby H. G. Wells\n"
                "‘And who is to say what walks the far side of the sky?’")
        assert epub.strip_heading(text, "The War of the Worlds", "The War of the Worlds",
                                  "by H. G. Wells").startswith("‘And who")

    def test_either_way_round(self):
        text = "by H. G. Wells\nThe War of the Worlds\nAnd then the story."
        assert epub.strip_heading(text, "", "The War of the Worlds",
                                  "by H. G. Wells") == "And then the story."

    def test_a_number_under_the_heading_is_not_the_title(self):
        """The second pass never takes a bare number: by then it's the book's own numbering
        inside the chapter, not a heading."""
        text = "CHAPTER 1\n1\nThe first section starts here."
        assert epub.strip_heading(text, "Chapter 1", "A Book", "by An Author") \
            == "1\nThe first section starts here."

    def test_leaves_prose_alone(self):
        text = "It was a bright cold day in April."
        assert epub.strip_heading(text, "Chapter 1") == text

    def test_a_section_named_after_its_own_words_keeps_them(self):
        """That name is the text's own opening, so every line below it "is part of the
        heading" — The Gunslinger's back-matter list lost three."""
        text = "Also by This Author\nFICTION\nA Novel\nAnd Another"
        assert epub.strip_heading(text, "Also by This Author FICTION A Novel…") == text

    def test_a_one_word_line_is_not_swallowed_by_a_long_title(self):
        """Its letters turn up inside the heading, and it's still the first line of the story."""
        text = "Dawn.\nThe rest of the chapter follows."
        assert epub.strip_heading(text, "A Wind Off the Downs at Dawn") == text

    def test_a_line_too_long_to_be_a_heading_stays(self):
        """Real headings run longer than they look — Rich Dad Poor Dad has one of 74
        characters — so the line has to be paragraph-length before it stops being one."""
        text = ("The opening line of this section runs on for far longer than any heading ever "
                "would, being an entire paragraph of prose that the extraction happened to name "
                "the section after in the absence of anything better\n"
                "and then the rest of it follows.")
        assert epub.strip_heading(text, text.split("\n")[0]) == text


class TestReadingBackASkippedSection:
    """A section extraction dropped can be read at the top of the book, which means fetching its
    words from the stored EPUB — the index keeps only its name, length and reason."""

    @pytest.fixture
    def added(self, client, tmp_path, isolated_books):
        """A book added the way an upload adds one, so its EPUB is on disk to re-read."""
        docs = [("ded.html", page("Dedication", 7)),
                ("note.html", page("Notice", 28)),
                ("c1.html", page("Chapter 1", 400))]
        nav = [("Dedication", "ded.html", []), ("Notice", "note.html", []),
               ("Chapter 1", "c1.html", [])]
        path = build(tmp_path, docs, nav)
        with open(path, "rb") as f:
            r = client.post("/api/books", data={"file": (f, "t.epub")},
                            content_type="multipart/form-data")
        return r.get_json()["book"]["id"]

    def test_the_index_lists_what_was_left_out_without_the_words(self, client, added):
        listed = books.find_book(added)["skipped"]
        assert [s["name"] for s in listed] == ["Dedication", "Notice"]
        assert all("text" not in s for s in listed)

    def test_the_words_are_fetched_from_the_epub(self, client, added):
        got = client.get(f"/api/books/{added}/skipped/1").get_json()
        assert got["name"] == "Notice" and got["words"] == 28
        assert got["text"].split() == [f"word{i}" for i in range(28)]

    def test_no_such_section(self, client, added):
        assert client.get(f"/api/books/{added}/skipped/9").status_code == 404
        assert client.get(f"/api/books/{added}/skipped/0").status_code == 200

    def test_unknown_book(self, client):
        assert client.get("/api/books/nope/skipped/0").status_code == 404

    def test_without_the_stored_epub(self, client, added):
        os.remove(books.book_dir(added, "book.epub"))
        r = client.get(f"/api/books/{added}/skipped/0")
        assert r.status_code == 400
        assert "EPUB" in r.get_json()["error"]

    def test_a_list_that_has_moved_on_is_refused(self, client, added):
        """Positional, so it's only the same list if the same extraction produced it. Reading out
        the wrong section would be a strange way to find out the heuristics had changed."""
        books.update_book(added, lambda b: b.update(
            skipped=[{"name": "Something else", "words": 3, "why": "x"}] + b["skipped"]))
        r = client.get(f"/api/books/{added}/skipped/0")
        assert r.status_code == 409
        assert "changed" in r.get_json()["error"]


class TestPuttingASectionBack:
    """A dropped section back in as a real chapter, where the book has it — an afterword, or a
    notice that belongs mid-book rather than read out at the top.

    A chapter's position is its number to everything else, so the whole point of these is that
    inserting renumbers positions and moves no files at all.
    """

    @pytest.fixture
    def added(self, client, tmp_path, isolated_books):
        """Three chapters with a 28-word notice between the first and the second."""
        docs = [("c1.html", page("Chapter 1", 400)),
                ("note.html", page("Notice", 28)),
                ("c2.html", page("Chapter 2", 400)),
                ("c3.html", page("Chapter 3", 400))]
        nav = [("Chapter 1", "c1.html", []), ("Notice", "note.html", []),
               ("Chapter 2", "c2.html", []), ("Chapter 3", "c3.html", [])]
        path = build(tmp_path, docs, nav)
        with open(path, "rb") as f:
            r = client.post("/api/books", data={"file": (f, "t.epub")},
                            content_type="multipart/form-data")
        return r.get_json()["book"]["id"]

    def put(self, client, bid, n=0):
        return client.post("/api/books/insert", json={"id": bid, "section": n})

    def names(self, bid):
        return [c["name"] for c in books.find_book(bid)["chapters"]]

    def test_it_lands_where_the_book_has_it(self, client, added):
        r = self.put(client, added)
        assert r.get_json()["at"] == 1
        assert self.names(added) == ["Chapter 1", "Notice", "Chapter 2", "Chapter 3"]
        # positions are what the page, the counts and the saved position all mean by a chapter
        assert [c["i"] for c in books.find_book(added)["chapters"]] == [0, 1, 2, 3]

    def test_no_file_is_renamed(self, client, added, isolated_books):
        """The reason this is affordable at all. Inserting at 1 in a book of 192 would otherwise
        move 191 text files and some 400 opus files, with a rollback path for a half-done job."""
        before = sorted(os.listdir(books.book_dir(added, "text")))
        self.put(client, added)
        after = sorted(os.listdir(books.book_dir(added, "text")))
        assert before == ["ch000.txt", "ch001.txt", "ch002.txt"]
        assert after == before + ["ch003.txt"]        # the new one, nothing moved
        keys = [books.chapter_key(c) for c in books.find_book(added)["chapters"]]
        assert keys == [0, 3, 1, 2]

    def test_the_section_s_words_are_what_gets_narrated(self, client, added):
        self.put(client, added)
        book = books.find_book(added)
        text = books.chapter_segments(book, 1)
        assert text and text[0].split() == [f"word{i}" for i in range(28)]
        assert book["chapters"][1]["words"] == 28

    def test_a_shifted_chapter_keeps_the_audio_it_had(self, client, added, monkeypatch):
        """Its parts are named after its storage number, so they neither move nor need finding
        again — which is what stops this being a re-narration of the whole book."""
        monkeypatch.setattr(books, "audio_seconds", lambda p: 5.0)
        segs = [{"file": books.audio_name(1, 0), "seconds": 5.0}]
        os.makedirs(books.book_dir(added, "audio"), exist_ok=True)
        with open(books.audio_file(added, 1, 0), "wb") as f:
            f.write(b"\0" * 32)
        books.update_book(added, lambda b: b["chapters"][1].update(state="ready", segments=segs,
                                                                  seconds=5.0))
        self.put(client, added)
        moved = books.find_book(added)["chapters"][2]
        assert moved["name"] == "Chapter 2" and moved["state"] == "ready"
        assert moved["segments"] == segs
        assert books.segments_on_disk(added, books.chapter_key(moved)) == segs

    def test_the_saved_position_moves_with_the_chapters(self, client, added):
        books.update_book(added, lambda b: b.update(
            position={"chapter": 2, "segment": 1, "offset": 30}))
        self.put(client, added)
        assert books.find_book(added)["position"] == {"chapter": 3, "segment": 1, "offset": 30}

    def test_a_position_above_the_insert_stays_where_it_is(self, client, added):
        books.update_book(added, lambda b: b.update(
            position={"chapter": 0, "segment": 0, "offset": 12}))
        self.put(client, added)
        assert books.find_book(added)["position"]["chapter"] == 0

    def test_anything_mid_render_is_invalidated(self, client, added):
        """A render thread started in the microsecond before this can't be found to be stopped,
        so the generation moves under it and it throws away whatever it makes."""
        was = books.find_book(added).get("gen", 0)
        self.put(client, added)
        assert books.find_book(added)["gen"] == was + 1

    def test_it_refuses_while_a_chapter_of_this_book_is_narrating(self, client, added):
        books.update_book(added, lambda b: b["chapters"][0].update(state="rendering"))
        r = self.put(client, added)
        assert r.status_code == 409 and "narrated" in r.get_json()["msg"]
        assert len(self.names(added)) == 3

    def test_it_refuses_while_a_run_is_going(self, client, added):
        books.update_book(added, lambda b: b.update(render_all={"running": True}))
        assert self.put(client, added).status_code == 409

    def test_it_refuses_while_a_chapter_of_this_book_is_queued(self, client, added,
                                                               monkeypatch):
        """A queued render has already been handed a position and only reads the book when the
        lock reaches it, so it would narrate whatever had moved into that position."""
        monkeypatch.setitem(books.render_state, "waiting", [(added, 2)])
        assert self.put(client, added).status_code == 409

    def test_another_book_in_the_engine_is_no_business_of_this(self, client, added,
                                                              monkeypatch):
        monkeypatch.setitem(books.render_state, "waiting", [("someone-else", 2)])
        assert self.put(client, added).get_json()["ok"] is True

    def test_the_same_section_twice_is_refused(self, client, added):
        assert self.put(client, added).get_json()["ok"] is True
        r = self.put(client, added)
        assert r.status_code == 409 and "already" in r.get_json()["msg"]
        assert self.names(added).count("Notice") == 1

    def test_a_list_that_has_moved_on_is_refused(self, client, added):
        books.update_book(added, lambda b: b.update(
            skipped=[{"name": "Something else", "words": 3, "why": "x"}] + b["skipped"]))
        r = self.put(client, added)
        assert r.status_code == 409 and "changed" in r.get_json()["msg"]

    def test_no_such_section(self, client, added):
        assert self.put(client, added, 9).status_code == 404

    def test_which_section_is_required(self, client, added):
        assert client.post("/api/books/insert", json={"id": added}).status_code == 400

    def test_unknown_book(self, client):
        assert self.put(client, "nope").status_code == 404

    def test_it_can_be_narrated_under_its_own_number(self, client, added, fake_tts):
        self.put(client, added)
        books.render_chapter(added, 1)
        c = books.find_book(added)["chapters"][1]
        assert c["state"] == "ready"
        assert [s["file"] for s in c["segments"]] == [books.audio_name(3, 0)]
        assert os.path.basename(fake_tts[0]["out"]) == "ch003-s00.opus"

    def test_leaving_it_out_again_costs_nothing(self, client, added):
        """It's a chapter like any other once it's in, ⊘ included — which is the way back if the
        position turns out to be wrong."""
        self.put(client, added)
        assert client.post("/api/books/skip",
                           json={"id": added, "chapter": 1}).get_json()["ok"]
        assert books.chapters_in(books.find_book(added)) == [
            c for c in books.find_book(added)["chapters"] if c["name"] != "Notice"]


class TestRescanWithASectionPutBack:
    """Re-reading the EPUB can't compare a section you put back with anything in the book, since
    it isn't in the book. The spine is matched against the chapters that came from the spine."""

    @pytest.fixture
    def added(self, client, tmp_path, isolated_books):
        self.docs = [("c1.html", page("Chapter 1", 400)),
                     ("note.html", page("Notice", 28)),
                     ("c2.html", page("Chapter 2", 400))]
        nav = [("Chapter 1", "c1.html", []), ("Notice", "note.html", []),
               ("Chapter 2", "c2.html", [])]
        self.tmp = tmp_path
        path = build(tmp_path, self.docs, nav)
        with open(path, "rb") as f:
            r = client.post("/api/books", data={"file": (f, "t.epub")},
                            content_type="multipart/form-data")
        bid = r.get_json()["book"]["id"]
        client.post("/api/books/insert", json={"id": bid, "section": 0})
        return bid

    def test_it_survives_where_it_was(self, client, added):
        r = client.post("/api/books/rescan", json={"id": added})
        d = r.get_json()
        assert d["ok"] and d["kept_audio"] and d["put_back"] == 1
        assert [c["name"] for c in books.find_book(added)["chapters"]] == [
            "Chapter 1", "Notice", "Chapter 2"]
        assert [c["i"] for c in books.find_book(added)["chapters"]] == [0, 1, 2]

    def test_and_every_chapter_keeps_the_number_its_files_are_under(self, client, added):
        """The text is rewritten by the rescan, so writing it by position would put chapter 2's
        prose into the notice's file."""
        client.post("/api/books/rescan", json={"id": added})
        chapters = books.find_book(added)["chapters"]
        assert [books.chapter_key(c) for c in chapters] == [0, 2, 1]
        with open(books.text_file(added, books.chapter_key(chapters[1]))) as f:
            assert f.read().split() == [f"word{i}" for i in range(28)]
        with open(books.text_file(added, books.chapter_key(chapters[2]))) as f:
            assert len(f.read().split()) == 400

    def test_a_book_that_has_changed_says_the_section_goes_with_it(self, client, added):
        """Then there's nothing to splice it into: the confirmation says so rather than dropping
        it quietly. It's one tap to put back."""
        other = build(self.tmp, [("c1.html", page("Chapter 1", 500)),
                                 ("c2.html", page("Chapter 2", 500))],
                      [("Chapter 1", "c1.html", []), ("Chapter 2", "c2.html", [])], "other.epub")
        shutil.copy(other, books.book_dir(added, "book.epub"))
        r = client.post("/api/books/rescan", json={"id": added})
        assert r.status_code == 409
        assert "put back" in r.get_json()["msg"]
        r = client.post("/api/books/rescan", json={"id": added, "confirm": True})
        assert r.get_json()["put_back"] == 0
        assert [c["name"] for c in books.find_book(added)["chapters"]] == ["Chapter 1",
                                                                          "Chapter 2"]


class TestPuttingASectionBackAtTheTop:
    @pytest.fixture
    def added(self, client, tmp_path, isolated_books):
        docs = [("ded.html", page("Dedication", 7)),
                ("c1.html", page("Chapter 1", 400)),
                ("c2.html", page("Chapter 2", 400))]
        nav = [("Dedication", "ded.html", []), ("Chapter 1", "c1.html", []),
               ("Chapter 2", "c2.html", [])]
        path = build(tmp_path, docs, nav)
        with open(path, "rb") as f:
            r = client.post("/api/books", data={"file": (f, "t.epub")},
                            content_type="multipart/form-data")
        return r.get_json()["book"]["id"]

    def test_it_takes_over_the_books_opening(self, client, added, monkeypatch):
        """The title and author are spoken at the top of the first chapter narrated, so a section
        put in above everything takes them — and whatever used to open the book has to lose them.
        Only that chapter's first part is re-made; the rest stays on disk."""
        started = []
        monkeypatch.setattr(books, "render_chapter", lambda b, i: started.append((b, i)))
        books.update_book(added, lambda b: b["chapters"][0].update(
            state="ready", segments=[{"file": books.audio_name(0, 0), "seconds": 5.0}]))
        r = client.post("/api/books/insert", json={"id": added, "section": 0})
        d = r.get_json()
        assert d["at"] == 0 and d["reopened"] == [1]
        chapters = books.find_book(added)["chapters"]
        assert [c["name"] for c in chapters][:2] == ["Dedication", "Chapter 1"]
        # pending, with its parts kept: render_chapter deletes only the one that went stale
        assert chapters[1]["state"] == "pending" and chapters[1]["segments"]
        assert started == [(added, 1)]

    def test_an_unnarrated_book_has_no_opening_to_move(self, client, added, monkeypatch):
        started = []
        monkeypatch.setattr(books, "render_chapter", lambda b, i: started.append((b, i)))
        assert client.post("/api/books/insert",
                           json={"id": added, "section": 0}).get_json()["reopened"] == []
        assert started == []


class TestASectionBackInsideAPart:
    @pytest.fixture
    def added(self, client, tmp_path, isolated_books):
        docs = [("c1.html", page("Chapter 1", 400)),
                ("note.html", page("Notice", 28)),
                ("c2.html", page("Chapter 2", 400))]
        nav = [("Part One", "c1.html", [("Chapter 1", "c1.html", [])]),
               ("Notice", "note.html", []),
               ("Part Two", "c2.html", [("Chapter 1", "c2.html", [])])]
        path = build(tmp_path, docs, nav)
        with open(path, "rb") as f:
            r = client.post("/api/books", data={"file": (f, "t.epub")},
                            content_type="multipart/form-data")
        return r.get_json()["book"]["id"]

    def test_it_joins_the_part_it_lands_in_front_of(self, client, added):
        """Or it would split that part in two in the folded list, and be announced as a part of
        its own — a section between two parts belongs to the one it introduces."""
        assert client.post("/api/books/insert",
                           json={"id": added, "section": 0}).get_json()["ok"]
        chapters = books.find_book(added)["chapters"]
        assert [c["name"] for c in chapters] == ["Part One · Chapter 1", "Part Two · Notice",
                                                 "Part Two · Chapter 1"]
        assert [p["part"] for p in books.book_parts(books.find_book(added))] == ["Part One",
                                                                                "Part Two"]
