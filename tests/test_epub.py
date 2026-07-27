"""EPUB extraction, against EPUBs built here rather than real books.

Real books are gitignored — they're copyrighted and large — so the fixtures are the smallest
files that still exercise what matters: the TOC nesting that turns into "Part · Chapter", the
front matter that gets dropped, and a flat TOC that legitimately has no parts to find.
"""
import os
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

    def test_short_sections_dropped_with_a_reason(self, tmp_path):
        docs = [("a.html", page("Tiny", 5)), ("b.html", page("Chapter 1", 400))]
        nav = [("Tiny", "a.html", []), ("Chapter 1", "b.html", [])]
        _meta, chapters, skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["Chapter 1"]
        assert "words" in next(s["why"] for s in skipped if s["name"] == "Tiny")

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

    def test_leaves_prose_alone(self):
        text = "It was a bright cold day in April."
        assert epub.strip_heading(text, "Chapter 1") == text


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
