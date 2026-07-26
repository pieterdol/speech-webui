"""EPUB extraction, against EPUBs built here rather than real books.

Real books are gitignored — they're copyrighted and large — so the fixtures are the smallest
files that still exercise what matters: the TOC nesting that turns into "Part · Chapter", the
front matter that gets dropped, and a flat TOC that legitimately has no parts to find.
"""
import zipfile

import pytest

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
        import speech
        _meta, chapters, _skipped = epub.extract(book)
        assert speech.part_of(chapters[0]["name"]) == "Part One"
        assert speech.label_number(chapters[3]["name"].split(speech.PART_SEP, 1)[1]) == 2

    def test_metadata(self, book):
        meta, _chapters, _skipped = epub.extract(book)
        assert meta["title"] == "Test Book"
        assert meta["author"] == "A Writer"


class TestFlatToc:
    """1984's shape: three entries, no children. There are no parts to find, and saying so is
    correct rather than a failure."""

    def test_no_part_prefixes(self, tmp_path):
        docs = [(f"c{i}.html", page(f"c{i}")) for i in range(3)]
        nav = [("PART I", "c0.html", []), ("PART II", "c1.html", []),
               ("PART III", "c2.html", [])]
        _meta, chapters, _skipped = epub.extract(build(tmp_path, docs, nav))
        assert [c["name"] for c in chapters] == ["PART I", "PART II", "PART III"]
        import speech
        assert all(speech.part_of(c["name"]) == "" for c in chapters)


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
