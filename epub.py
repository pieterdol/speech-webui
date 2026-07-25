"""Pull narratable text out of an EPUB.

An EPUB is a zip: META-INF/container.xml points at an .opf, whose <spine> gives the reading
order and whose <manifest> maps ids to files. The .ncx (or an EPUB 3 nav doc) supplies chapter
titles. Only the standard library plus bs4.

The output is what a voice should read: paragraphs of prose, with the covers, colophons,
advertisements and part-title pages left out.
"""
import re
import zipfile
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

# Measured with Kokoro af_heart at speed 1.0 on a 250-word passage.
WORDS_PER_MIN = 146

DROP_TAGS = ["script", "style", "svg", "figure", "figcaption", "table", "sup", "nav"]
# Sections that are apparatus rather than the book. Matched against the file name and title.
SKIP_HINTS = re.compile(r"cover|copyright|toc|contents|colophon|advert|buylink|backad|"
                        r"praise|also_by|alsoby|dedication|epigraph|teaser|excerpt|signup|"
                        r"newsletter|titlepage|halftitle", re.I)
# Below this a section is a part-title page or a stray line, not something to narrate.
MIN_WORDS = 120


def _opf_path(z):
    soup = BeautifulSoup(z.read("META-INF/container.xml"), "xml")
    return soup.find("rootfile")["full-path"]


def _text_of(z, href):
    """(heading, prose) for one spine document, one paragraph per line."""
    try:
        raw = z.read(href)
    except KeyError:
        return "", ""
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(DROP_TAGS):
        t.decompose()
    # Two traps when flattening inline markup, both of which produce audible mistakes:
    #   separator=" " turns "L<span class=smallcaps>ORD</span>" into "L ORD" — two words;
    #   strip=True strips each fragment, so "for <i>The Stand</i>" becomes "forThe Stand".
    # So join with nothing, keep the source's own whitespace, and tidy up once at the end.
    clean = lambda el: re.sub(r"\s+", " ", el.get_text("")).strip()
    h = soup.find(["h1", "h2", "h3"])
    heading = clean(h) if h else ""
    blocks = []
    for p in soup.find_all(["p", "h1", "h2", "h3", "blockquote", "li"]):
        s = clean(p)
        if s:
            blocks.append(s)
    if not blocks:                      # some books wrap everything in bare divs
        s = clean(soup)
        blocks = [s] if s else []
    return heading, "\n".join(blocks)


def extract(path):
    """-> (meta, chapters, skipped).

    chapters: [{name, words, text}] in reading order.
    skipped:  [{name, words, why}] so the UI can show what was left out and let it back in.
    """
    z = zipfile.ZipFile(path)
    opf = _opf_path(z)
    soup = BeautifulSoup(z.read(opf), "xml")

    def meta_of(tag):
        el = soup.find(tag)
        return el.get_text(strip=True) if el else ""

    meta = {"title": meta_of("title") or "Untitled",
            "author": meta_of("creator"),
            "language": (meta_of("language") or "").lower()}

    manifest = {i["id"]: unquote(urljoin(opf, i["href"])) for i in soup.find_all("item")
                if i.get("id") and i.get("href")}
    spine = [manifest[r["idref"]] for r in soup.find_all("itemref")
             if r.get("idref") in manifest]

    # Walk the TOC as a tree, not a flat list. Novels in parts number their chapters from 1
    # within each part — The Institute has four "Chapter 1"s — so a child's label only makes
    # sense with its parent's: "The Night Knocker · Chapter 1".
    titles = {}
    ncx = next((h for h in manifest.values() if h.endswith(".ncx")), None)
    if ncx:
        nav = BeautifulSoup(z.read(ncx), "xml")

        def walk(points, parent=""):
            for p in points:
                lbl = p.find("navLabel", recursive=False)
                c   = p.find("content", recursive=False)
                kids = p.find_all("navPoint", recursive=False)
                label = lbl.get_text(strip=True) if lbl else ""
                if c and c.get("src") and label:
                    href = unquote(urljoin(opf, c["src"])).split("#")[0]
                    titles[href] = f"{parent} · {label}" if parent else label
                if kids:
                    walk(kids, label or parent)

        navmap = nav.find("navMap")
        if navmap:
            walk(navmap.find_all("navPoint", recursive=False))

    chapters, skipped = [], []
    for href in spine:
        heading, body = _text_of(z, href)
        words = len(body.split())
        # A section with no entry in the table of contents and no heading — an epigraph, say —
        # would otherwise be listed as "fm00.html". Its opening words say far more.
        opening = " ".join(body.split()[:6])
        name = titles.get(href) or heading or (f"{opening}…" if opening else
                                               href.rsplit("/", 1)[-1])
        why = ("looks like front or back matter" if SKIP_HINTS.search(href) or SKIP_HINTS.search(name)
               else f"only {words} words" if words < MIN_WORDS else None)
        if why:
            skipped.append({"name": name, "words": words, "why": why})
            continue
        chapters.append({"name": name, "words": words, "text": body})
    return meta, chapters, skipped


def cover(path):
    """The cover image bytes, or None.

    Always follow what the book declares. Guessing by filename picks the wrong image often
    enough to matter — The Institute ships six `buylink_*_cover.jpg` files, which are the
    covers of *other* novels advertised in the back matter.
    """
    z = zipfile.ZipFile(path)
    opf = _opf_path(z)
    soup = BeautifulSoup(z.read(opf), "xml")
    items = {i["id"]: unquote(urljoin(opf, i["href"]))
             for i in soup.find_all("item") if i.get("id") and i.get("href")}

    def read(href):
        try:
            return z.read(href)
        except KeyError:
            return None

    # EPUB 3 declares it on the manifest item
    for i in soup.find_all("item"):
        if "cover-image" in (i.get("properties") or ""):
            data = read(unquote(urljoin(opf, i["href"])))
            if data:
                return data
    # EPUB 2 points at a manifest id from a <meta>
    m = soup.find("meta", attrs={"name": "cover"})
    if m and items.get(m.get("content")):
        data = read(items[m["content"]])
        if data:
            return data
    # Older books only have a guide entry pointing at a page that holds the image
    g = soup.find("reference", attrs={"type": "cover"})
    if g and g.get("href"):
        page = unquote(urljoin(opf, g["href"])).split("#")[0]
        raw = read(page)
        if raw:
            ps = BeautifulSoup(raw, "html.parser")
            el = ps.find("img") or ps.find("image")
            src = el and (el.get("src") or el.get("xlink:href") or el.get("href"))
            if src:
                data = read(unquote(urljoin(page, src)))
                if data:
                    return data
    return None


def minutes(words):
    return words / WORDS_PER_MIN


def strip_heading(text, name):
    """Chapter files usually open with the chapter number or title as its own line, which
    narrates as 'Nine. Led by...'. Drop that first line when it's just the heading."""
    lines = text.split("\n")
    if not lines:
        return text
    first = lines[0].strip()
    if len(first) <= 60 and (first.rstrip(".") == (name or "").rstrip(".")
                             or re.fullmatch(r"[\dIVXLC]+\.?", first)
                             or re.fullmatch(r"(?i)chapter\s+[\dIVXLC]+\.?", first)):
        return "\n".join(lines[1:]).strip()
    return text
