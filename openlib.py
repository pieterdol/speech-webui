"""What a book is about, from Open Library.

The only part of this app that talks to a machine that isn't this one. A title and an author go
to openlibrary.org when a book is added, and a sentence or two comes back to sit above the
chapter list; speech, the model and the library itself stay here. Nothing depends on the answer,
so a book with no description, a bad match or no network is an ordinary outcome and never an
error — see fetch_description in books.py, which runs this off the upload.

Two calls: a search for the work, then the work itself, since the search index doesn't carry
descriptions. The work key is kept with the description because the match is a guess — a title
and an author are all we have to go on, and Open Library will happily return a different edition
of a different book.
"""
import json
import os
import re
import urllib.parse
import urllib.request

API = "https://openlibrary.org"
# Whether adding a book looks it up by itself. `OPENLIBRARY=0` and nothing leaves this machine
# unless you ask for it on a book, which ↻ in ⚙ still does — tapping it is the request. Read
# once at startup, like the other settings, so turning it off takes a restart.
AUTOMATIC = os.environ.get("OPENLIBRARY", "1").strip().lower() not in ("0", "no", "off", "")
TIMEOUT = 12
# Two or three sentences: enough to remember what a book is, short enough to read on a phone
# above the chapter list. Their descriptions run to several paragraphs and a page of them would
# be a wall of text where a reminder was wanted.
DESCRIPTION_CHARS = 600
# Shorter than this isn't a description, it's a scrap — a genre tag or a note to editors, which
# is what a duplicate work record often carries instead of a blurb.
USEFUL_CHARS = 40
# Where a title stops and its subtitle starts. An EPUB's metadata title carries the subtitle and
# the catalogue's usually doesn't, and searching the whole of "Moby Dick; Or, The Whale" or
# "Flatland: A Romance of Many Dimensions" finds nothing at all.
_SUBTITLE = re.compile(r"\s*[:;]\s|\s+/\s+")
# What contributors hang on the end of a description: where the blurb came from, and the
# markdown link references that go with it. Neither is worth reading.
_CREDIT = re.compile(r"^\s*(\(?\[?source\b|--|—|\[\d+\]:|https?://)", re.I)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\[\d+\]")


def _get(url):
    """The JSON at a URL, or None. Open Library asks callers to name themselves."""
    request = urllib.request.Request(url, headers={"User-Agent": "speech-webui (local reader)"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def shorten(text):
    """A description cut down to something worth showing.

    Paragraph at a time rather than by characters alone, because the first paragraph is often a
    tagline of half a dozen words — and stopping at the credit, which is where the blurb ends
    and the note about where it came from begins.
    """
    kept = []
    for para in re.split(r"\n\s*\n|\n", text or ""):
        para = _LINK.sub(lambda m: m.group(1) or m.group(2) or "", para).strip()
        if not para or _CREDIT.match(para):
            break
        kept.append(para)
        if sum(len(p) for p in kept) >= DESCRIPTION_CHARS:
            break
    out = " ".join(kept).strip()
    if len(out) <= DESCRIPTION_CHARS:
        return out
    # Cut at a word, and only put the ellipsis on when something was actually dropped.
    return out[:DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def attempts(title, author=""):
    """The searches to try, in the order worth trying them.

    The author narrows it a great deal — every catalogue has a dozen books called Dark Matter —
    but a name spelled differently there finds nothing at all, and a subtitle the catalogue
    doesn't use finds nothing either. So the full title with the author first, then the same
    without the subtitle, then that on its own.
    """
    title, author = (title or "").strip(), (author or "").strip()
    names = [n for n in (title, _SUBTITLE.split(title)[0].strip()) if n]
    # Narrowest first, loosest last: both titles with the author, then the shorter title on its
    # own. The full title without an author is skipped — if the author didn't find it, a longer
    # title without one won't either.
    tried, out = set(), []
    for name, who in [(n, author) for n in names] + [(names[-1] if names else "", "")]:
        if name and (name, who) not in tried:
            tried.add((name, who))
            out.append((name, who))
    return out


def work_key(title, author=""):
    """The Open Library work that best matches one search, as "/works/OL…W", or ""."""
    query = {"title": title, "limit": "1", "fields": "key"}
    docs = (_get(f"{API}/search.json?" + urllib.parse.urlencode(
        query | ({"author": author} if author else {}))) or {}).get("docs") or []
    return docs[0]["key"] if docs and docs[0].get("key", "").startswith("/works/") else ""


def description_of(work):
    """The description on a work record, shortened, or ""."""
    text = (_get(f"{API}{work}.json") or {}).get("description")
    if isinstance(text, dict):                 # some records wrap it, some don't
        text = text.get("value")
    return shorten(text or "")


def describe(title, author=""):
    """-> (description, work key), both empty when there's nothing to be had.

    Each search is followed through to the work it names, because a search that matches is not
    the same as a description that exists: a public-domain title has a dozen work records and
    most of them are bare. The first one carrying something worth reading wins.
    """
    for name, who in attempts(title, author):
        work = work_key(name, who)
        if not work:
            continue
        text = description_of(work)
        if len(text) >= USEFUL_CHARS:
            return text, work
    return "", ""
