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
# "gutenberg" covers both ends of a Project Gutenberg text — the header page and the 343-word
# licence — which are otherwise chapters: announced, narrated, and given a marker in the .m4b.
# A book *about* Gutenberg loses its chapters to this, and the Left out panel is where you'd
# see that and put them back.
SKIP_HINTS = re.compile(r"cover|copyright|toc|contents|colophon|advert|buylink|backad|"
                        r"praise|also_by|alsoby|dedication|epigraph|teaser|excerpt|signup|"
                        r"newsletter|titlepage|halftitle|gutenberg|transcriber", re.I)
# Below this a section is a part-title page or a stray line, not something to narrate.
MIN_WORDS = 120
# Longest a line can be and still be a chapter's heading rather than the first line of its prose.
# Real ones run longer than they look: Rich Dad Poor Dad has "Chapter Four: Lesson 4: The History
# of Taxes and the Power of Corporations", at 74.
HEADING_CHARS = 100
# And how many lines of it there can be. A page sets the number and the title as separate blocks
# often enough to matter; three is room for that and a subtitle, and a floor under how much of a
# chapter a bad match could ever eat.
HEADING_LINES = 3
# What extract hangs on a name it had to make out of the section's own first words. A title from
# the contents never ends in one, and it's how the two rules that must not treat such a name as a
# heading find them: strip_heading, since that name matches the very text it was taken from, and
# the announcement, which would read the opening of the chapter out twice.
OPENING_NAME = "…"
# How a part's name is joined to a chapter's own, here and everywhere downstream.
PART_SEP = " · "
# A part-title page and the chapter under it can share one spine document, with only the part in
# the contents: The War of the Worlds sets "BOOK ONE THE COMING OF THE MARTIANS" over "I. THE EVE
# OF THE WAR." Then the chapter goes in under the part's name, its own heading is left in the
# prose to be read as "eye", and it announces the same thing every other chapter of book one does.
# The line under the part is the chapter's own heading when it opens with a number.
_NUMBERED_HEADING = re.compile(r"(?i)^(?:chapter|part|book|hoofdstuk|deel)?\s*[\dIVXLC]+\b")


def _opf_path(z):
    soup = BeautifulSoup(z.read("META-INF/container.xml"), "xml")
    return soup.find("rootfile")["full-path"]


def _pieces(z, href, entries=()):
    """[(label, prose)] for one spine document, one paragraph per line.

    Usually a document is one chapter and this returns one piece, whose label is "" for extract
    to name from the TOC or the heading. But a book can pack twenty chapters into one file and
    address each from the TOC by fragment — `Section0001.xhtml#heading_id_3` — and then the
    document has to be cut at those anchors or the whole file narrates as one 20,000-word
    chapter under the last label in it. `entries` is [(fragment, label)] for this file in TOC
    order; the ones carrying a fragment are the cuts.
    """
    try:
        raw = z.read(href)
    except KeyError:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(DROP_TAGS):
        t.decompose()
    # Two traps when flattening inline markup, both of which produce audible mistakes:
    #   separator=" " turns "L<span class=smallcaps>ORD</span>" into "L ORD" — two words;
    #   strip=True strips each fragment, so "for <i>The Stand</i>" becomes "forThe Stand".
    # So join with nothing, keep the source's own whitespace, and tidy up once at the end.
    clean = lambda el: re.sub(r"\s+", " ", el.get_text("")).strip()

    # Position of every element in the document, so a block of prose can be placed against the
    # anchors without changing how the blocks themselves are found.
    order = {id(el): n for n, el in enumerate(soup.descendants) if getattr(el, "name", None)}
    cuts = []
    for frag, label in entries:
        el = soup.find(id=frag) if frag else None
        if el is not None and id(el) in order:
            cuts.append((order[id(el)], label))
    cuts.sort()
    # The text before the first anchor is a chapter of its own — the prologue, or the chapter
    # the file-level TOC entry names. The last such entry wins, not the first: a part and the
    # chapter that opens it often point at the same file, parent before child, and "Part One ·
    # Chapter 1" is the better name of the two.
    lead = next((lbl for frag, lbl in reversed(entries) if not frag), "")
    pieces = [[lead, []]] + [[lbl, []] for _, lbl in cuts]

    blocks = soup.find_all(["p", "h1", "h2", "h3", "blockquote", "li"])
    for b in blocks:
        text = clean(b)
        if not text:
            continue
        pos = order.get(id(b), 0)
        at = sum(1 for c, _lbl in cuts if c <= pos)
        pieces[at][1].append(text)
    if not any(body for _lbl, body in pieces):   # some books wrap everything in bare divs
        text = clean(soup)
        if text:
            pieces[0][1] = [text]
    if not cuts:
        h = soup.find(["h1", "h2", "h3"])
        heading = clean(h) if h else ""
        return [(pieces[0][0] or heading, "\n".join(pieces[0][1]))]
    return [(lbl, "\n".join(body)) for lbl, body in pieces]


def extract(path):
    """-> (meta, chapters, skipped).

    chapters: [{name, words, text}] in reading order.
    skipped:  [{name, words, why, at, text}] so the UI can show what was left out — and so a piece
              of it can be read back or put in as a chapter, which is why it keeps its text and
              the position it would have had. The index stores all but the text: prose belongs in
              text/, not in books.json.
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
    #
    # The fragment is kept, not discarded: a book that holds several chapters per file names
    # them "Section0001.xhtml#heading_id_3", and dropping the anchor collapses every chapter in
    # the file onto one entry. Per file, in TOC order, so _pieces can cut the document.
    toc = {}
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
                    src  = unquote(urljoin(opf, c["src"]))
                    href, _, frag = src.partition("#")
                    toc.setdefault(href, []).append(
                        (frag, f"{parent} · {label}" if parent else label))
                if kids:
                    walk(kids, label or parent)

        navmap = nav.find("navMap")
        if navmap:
            walk(navmap.find_all("navPoint", recursive=False))

    chapters, skipped = [], []
    for href in spine:
        entries = toc.get(href, [])
        pieces = _pieces(z, href, entries)
        # A chapter the TOC points at by name is a chapter, however short — Dan Brown writes
        # some of 90 words. The length rule is there to drop part-title pages and stray lines,
        # which is a question about untitled sections.
        titled = any(frag for frag, _lbl in entries)
        for label, body in pieces:
            words = len(body.split())
            # A section with no entry in the table of contents and no heading — an epigraph,
            # say — would otherwise be listed as "fm00.html". Its opening words say far more.
            opening = " ".join(body.split()[:6])
            name = label or (f"{opening}{OPENING_NAME}" if opening
                            else href.rsplit("/", 1)[-1])
            why = ("looks like front or back matter"
                   if SKIP_HINTS.search(href) or SKIP_HINTS.search(name)
                   else f"only {words} words" if words < MIN_WORDS and not titled
                   else "no text in it" if not words else None)
            if why:
                # Where it would have gone: the number of chapters kept so far is exactly the
                # position it would hold. That's what lets a section be put back where the book
                # has it rather than only at the top or the end — and it's free here, where the
                # spine is being walked in order anyway.
                skipped.append({"name": name, "words": words, "why": why, "text": body,
                                "at": len(chapters)})
                continue
            entry = {"name": name, "words": words, "text": body}
            # A book that sets each chapter's title on a page of its own lists every chapter
            # twice. The Gunslinger has "Chapter 1: The Gunslinger" with two words in it, then
            # the chapter itself under the heading its own page carries, "CHAPTER 1" — two rows
            # in the library, two markers in the .m4b, and an announcement for each. The page is
            # the chapter's heading, and the contents' name for it is the better of the two.
            if chapters and _is_title_page(chapters[-1]):
                page = chapters.pop()
                entry.update(name=page["name"], text=page["text"] + "\n" + body)
                entry["words"] = len(entry["text"].split())
            # After the fold, because the page that came in may have been a *part's* title and
            # not this chapter's — and then the chapter's own heading is the line under it.
            entry["name"] += _own_heading(entry["text"], entry["name"])
            chapters.append(entry)
    return meta, chapters, skipped


def _own_heading(body, name):
    """The chapter's own heading joined on as a part would be, or "".

    For a section the contents names after the part-title line it opens with: the line under
    that one is the chapter, when it opens with a number and is short enough to be a heading.
    Joined rather than replacing, because the part is real — it's the part that page announces.
    """
    lines = [line.strip() for line in body.split("\n")[:2]]
    if PART_SEP in name or name.endswith(OPENING_NAME) or len(lines) < 2:
        return ""
    if _norm(lines[0]) != _norm(name) or len(lines[1]) > HEADING_CHARS:
        return ""
    return PART_SEP + lines[1] if _NUMBERED_HEADING.match(lines[1]) else ""


def _is_title_page(chapter):
    """Whether a section is nothing but the title of the chapter after it: one line's worth of
    text, saying what the contents calls it.

    A name made from the section's own first words is excluded, being no title — it would say
    the same thing about any short section at all, an epigraph included.
    """
    if chapter["name"].endswith(OPENING_NAME) or len(chapter["text"]) > HEADING_CHARS:
        return False
    text = _norm(chapter["text"])
    return bool(text) and text in _norm(chapter["name"])


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


def _norm(text):
    """A heading with nothing in it but its letters and digits, for comparing one written two
    ways: "Chapter 1:The Oracle" and "Chapter 1: The Oracle" are the same heading."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def strip_heading(text, name):
    """Drop the chapter's own heading off the top of its text, where the spoken lead-in says it
    better — left in, it narrates as 'Nine. Led by...', and now that a title is announced it
    would be read twice over.

    The page and the contents rarely agree word for word. Case differs: Eragon's contents say
    "Prologue: Shade of Fear" where the page shouts it. And the page usually sets the heading as
    two blocks where the contents joins them — "Chapter One" over "LESSON 1: THE RICH DON'T WORK
    FOR MONEY", or "THE GUNSLINGER" under a contents entry reading "Chapter 1: The Gunslinger".
    So a leading line comes off when the heading contains it, punctuation and case ignored, and
    the first line that isn't part of the heading stops it.

    Containment one way only. A line the heading contains is the heading; a line that contains
    the heading is prose that opens with the title's own words — "The long walk home began at
    dawn" under a chapter called "The Long Walk Home" — and stays.

    The name is matched a piece at a time, the part and the chapter's own heading each on their
    own, because a page carries whichever of them it happens to print: usually just the chapter,
    and both where a part-title page opens the file.
    """
    lines = text.split("\n")
    # A section named after its own first words is named after the very lines below, so every
    # one of them "is part of the heading" — that name is no heading at all.
    pieces = [] if (name or "").endswith(OPENING_NAME) else \
        [p for p in (_norm(x) for x in (name or "").split(PART_SEP)) if p]
    covered = [0] * len(pieces)
    taken = 0
    while taken < min(len(lines), HEADING_LINES):
        line = lines[taken].strip()
        if len(line) > HEADING_CHARS:
            break
        norm = _norm(line)
        # Three characters at least. Below that a line is a number, and a number is the heading
        # only at the very top: 11/22/63 numbers the sections inside a chapter, so under
        # "CHAPTER 1" sits a "1" that belongs to the prose — take it off and the first section
        # of every chapter is the only one without its number.
        hit = next((j for j, p in enumerate(pieces) if len(norm) >= 3 and norm in p), None)
        if hit is not None:
            covered[hit] += len(norm)
        elif taken == 0 and (re.fullmatch(r"[\dIVXLC]+\.?", line)
                             or re.fullmatch(r"(?i)chapter\s+[\dIVXLC]+\.?", line)):
            covered = [len(p) for p in pieces]    # a number is all of the heading there is
        else:
            break
        taken += 1
    # And what came off has to be most of the piece it matched, not a few of its letters:
    # "Dawn." is a line of the story under a chapter called "A Wind Off the Downs at Dawn", and
    # every word of it appears in that title.
    if any(0 < c * 2 < len(p) for c, p in zip(covered, pieces)):
        return text
    # A part-title page kept as a chapter — The Gunslinger has "THE GUNSLINGER" and nothing
    # else — is all heading. Emptying it would leave a chapter with no segments, which is a
    # chapter with no announcement either, and a marker of no length in the .m4b.
    rest = "\n".join(lines[taken:]).strip()
    return rest if taken and rest else text
