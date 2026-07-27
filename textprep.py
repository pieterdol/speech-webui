"""Turning written text into something worth reading out loud, and cutting it into pieces
an engine can speak. Shared by chat, the studio and book narration."""
import re

# Neither engine takes a pronunciation override, so hard words are respelled phonetically on
# the way in. Kokoro's documented [word](/phonemes/) form belongs to the KPipeline package and
# does nothing here: kokoro_onnx offers only all-or-nothing is_phonemes, so the markup is read
# out loud as "slash m stress u lengthen v i z slash". Respelling is the whole toolkit.
#
# These apply everywhere. A book also carries its own map — the names in a novel are nobody
# else's problem — which respell() takes as `extra` and which wins where the two disagree.
RESPELL = {
    "Pieter": "Peter",
    "movies": "movees",     # espeak clips the -ies to "movis"
}

# How many a book may carry, and how long each side may be. A cap because the map is typed in
# from a phone and every entry is a regex pass over every chunk of every render.
RESPELL_MAX   = 200
RESPELL_CHARS = 80

# Abbreviations written short and meant to be heard in full. The full stop is the whole
# problem: an engine reads "Mr. Halloway" as "mister", then a sentence break, then the name —
# a pause the text never asked for. Writing the word out removes the stop along with it.
#
# This runs after sentence splitting, so _ABBREV has already done its own job of not cutting
# the sentence at "Mr."; by the time a chunk reaches an engine the abbreviation is gone.
#
# A title always sits in front of a name, so its full stop is never the end of a sentence and
# always comes off.
HONORIFICS = {"Mr": "Mister", "Mrs": "Missus", "Ms": "Miz", "Dr": "Doctor",
              "Prof": "Professor"}
# These can fall at the end of a sentence — "…and Sammy Jr." — where the one full stop is
# doing both jobs. Taking it off would run the sentence into the next one, so it's kept when
# what follows looks like a new sentence and dropped when it doesn't.
SPOKEN_ABBREV = {"Jr": "Junior", "Sr": "Senior", "vs": "versus", "etc": "et cetera",
                 "e.g": "for example", "i.e": "that is", "approx": "approximately"}
# Left alone on purpose. "St." is Saint before a name and Street after one, and this has no
# way to tell which; "fig.", "al." and "inc." are rare enough in prose that guessing wrong
# costs more than the abbreviation does. They stay in _ABBREV, so they still don't split a
# sentence — they're just spoken as written.


# The number words, for reading a chapter heading that spells its number out — "Chapter
# Twenty-One". Only that direction is needed: digits handed to an engine are read in whatever
# language it speaks, so nothing here turns a number into words.
ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


# Dates and pairs written with a slash. Read as written, "11/22/63" comes out as "eleven slash
# twenty-two slash sixty-three": the slash is a writing convention, not a sound. Dropping it for
# a comma is all this does — the digits are left to the engine, which says "elf, tweeëntwintig"
# for a Dutch voice and "eleven, twenty-two" for an English one, and reads a four-digit group as
# a year in both. Spelling the numbers out here would mean spelling them out in one language.
#
# The comma is the beat between the groups; without it they run together into one long number.
# A leading zero is the one thing the engine gets wrong on its own — "02" is read "zero two" —
# so it comes off.
#
# Which group is the month is never guessed at, which a spoken-out date would have to: "10/7" is
# October 7th in an American book and July 10th in a Dutch one.
#
# Groups are digits only, so "and/or" and "Jake/George" are never touched, and a slash on either
# side rules out a URL or a path.
_SLASH_NUMBERS = re.compile(r"(?<![\w/.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\w/])")

# The same date written with hyphens — "10-02-1986". A hyphen needs a much narrower rule than a
# slash, because between numbers it is usually a range and not a separator: "1914-1918", "pages
# 10-20", "the 2020-21 season", "MS-13". So only the two forms carrying a four-digit year count
# as a date, day-first or ISO, and everything else keeps its hyphen. A hyphen on either side
# rules out a longer chain of digits like an ISBN.
_HYPHEN_DATE = re.compile(r"(?<![\w-])(?:(\d{1,2})-(\d{1,2})-(\d{4})"
                          r"|(\d{4})-(\d{1,2})-(\d{1,2}))(?![\w-])")

def _spoken_groups(match):
    """The groups as digits, slash or hyphen gone. "0" is a group like any other, so this tests
    for None rather than for falsiness — the alternation above leaves half its groups unset."""
    groups = [g for g in match.groups() if g is not None]
    # Two single digits are a fraction far more often than a date, and "one two" is worse than
    # the slash. Three groups are a date whatever their sizes.
    if len(groups) == 2 and all(len(g) == 1 for g in groups):
        return match.group(0)
    return ", ".join(g.lstrip("0") or "0" for g in groups)


def _abbrev_re(abbr):
    """Matches the abbreviation, never inside a longer word — so "Mr." matches and "Mrs."
    doesn't, whichever order the two are tried in.

    Case is not ignored, and the all-caps form has to carry its full stop. Both rules are
    there because a two-letter capital without a stop is almost always an initialism for
    something else: across three real books, "the DR" was the Dominican Republic, "MS-13" a
    gang, and "SR 92" a state route — five occurrences of that last one. An all-caps
    heading like "MR. HALLOWAY" does have the stop, so it still expands. Lowercase "ms" isn't
    accepted at all: it's milliseconds far more often than it's an honorific.
    """
    natural = sorted({abbr, abbr.capitalize()}, key=len, reverse=True)
    alts = [re.escape(f) + r"\.?" for f in natural]
    if abbr.upper() not in natural:
        alts.append(re.escape(abbr.upper()) + r"\.")
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(alts))


_HONORIFIC = [(_abbrev_re(a), full) for a, full in HONORIFICS.items()]
_SPOKEN    = [(_abbrev_re(a), full) for a, full in SPOKEN_ABBREV.items()]
# "No." is a number only when a number follows it. Everywhere else it's the ordinary word,
# which is why it can't go in either table.
_NUMBER_OF = re.compile(r"(?<!\w)No\.(?=\s*\d)")


def _expand(match, full):
    """Write the abbreviation out, keeping its full stop only when that stop is also ending a
    sentence — nothing after it, or a capital letter starting the next one."""
    if not match.group(0).endswith("."):
        return full
    after = match.string[match.end():].lstrip(" \t\"'”’)")
    return full + ("." if not after or after[0].isupper() else "")


def _word_re(src):
    """The whole word, whatever its case. One helper because the substitution below and the
    search that decides which audio a changed map invalidates have to agree exactly — a
    difference between them would either re-make audio that was fine or leave audio that isn't.

    \\b can't match beside a non-word character, so a key like "Ph.D." never fires. That's a
    limitation of matching words rather than something to work around here.
    """
    return re.compile(rf"\b{re.escape(src)}\b", re.IGNORECASE)


def respell(text, extra=None):
    """`extra` is one book's own map, applied on top of the global one and winning where both
    name the same word. None — every caller outside book narration — is exactly the global map.
    """
    for pattern, full in _HONORIFIC:
        text = pattern.sub(full, text)
    for pattern, full in _SPOKEN:
        text = pattern.sub(lambda m, f=full: _expand(m, f), text)
    text = _NUMBER_OF.sub("number", text)
    text = _SLASH_NUMBERS.sub(_spoken_groups, text)
    text = _HYPHEN_DATE.sub(_spoken_groups, text)
    # A replacement is put in through a function, not as re.sub's template: a reader typing
    # "AC\DC" or "\1" would otherwise have it read as a backreference and raise re.error deep
    # inside a render thread. A callable is never interpreted.
    #
    # Merged rather than chained per map, so a book overriding a global word replaces that rule
    # instead of running after it. The order is global keys first, then the book's own — which
    # means a book key matching some global rule's *output* still fires on it. Deterministic,
    # occasionally surprising, and what the repair scan compares against anyway.
    for src, dst in (RESPELL | (extra or {})).items():
        text = _word_re(src).sub(lambda m, d=dst: d, text)
    return text


def clean_respell(mapping):
    """One book's map as it goes to disk: no whitespace, no empties, no duplicates, capped.

    Case-insensitive de-duplication because the match is: storing "Vermeer" and "vermeer" would
    be two rules firing on the same word, the second one over the first one's output. An empty
    *replacement* is kept — it means "don't say this at all", which is a real thing to want for a
    footnote marker; an empty key is dropped, having nothing to match."""
    out = {}
    if not isinstance(mapping, dict):
        return out
    for src, dst in mapping.items():
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        src, dst = src.strip()[:RESPELL_CHARS], dst.strip()[:RESPELL_CHARS]
        if not src or src.casefold() in {k.casefold() for k in out}:
            continue
        out[src] = dst
        if len(out) >= RESPELL_MAX:
            break
    return out


def respell_diff(old, new):
    """What changed between two maps: (added, edited, removed) as sorted key lists.

    Compared case-folded, since re-casing a key that keeps its value changes no audio — the
    match ignores case either way. For reporting, and for deciding there's nothing to do."""
    a = {k.casefold(): v for k, v in (old or {}).items()}
    b = {k.casefold(): v for k, v in (new or {}).items()}
    return (sorted(k for k in b if k not in a),
            sorted(k for k in b if k in a and a[k] != b[k]),
            sorted(k for k in a if k not in b))


# ---- turning a written reply into something worth listening to ----
# Kokoro reads punctuation literally, so "**important**" becomes "asterisk asterisk important"
# and a URL becomes a spelled-out mess. Lives here rather than in the browser so the streamed
# and the manual "speak this reply" paths can't drift apart.
_MD = [
    (re.compile(r"```.*?```", re.S), " "),          # code blocks are unlistenable
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links: keep the words, drop the URL
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),
    (re.compile(r"(\*\*|__|~~|\*|_)"), ""),
    (re.compile(r"\n{2,}"), "\n"),
]

def speech_text(text):
    for pattern, repl in _MD:
        text = pattern.sub(repl, text or "")
    return text.strip()

# Sentence boundary: closing punctuation, optional quote/bracket, then whitespace. A decimal
# ("3.5") has no space after the dot, so it never matches.
_BOUNDARY = re.compile(r'(?<=[.!?…])["\'”’)\]]*(\s+)')
# Periods that end an abbreviation rather than a sentence.
_ABBREV = {"e.g", "i.e", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
           "fig", "approx", "no", "al", "inc", "ltd"}

def _is_real_end(text, dot):
    """dot = index just past the sentence-ending punctuation."""
    if text[dot - 1] != ".":
        return True                      # ! and ? don't have this problem
    word = re.search(r"([A-Za-z.]+)\.$", text[:dot])
    if not word:
        return True
    w = word.group(1).rstrip(".").lower()
    return not (w in _ABBREV or len(w) == 1)   # "J. Smith" shouldn't split either

def cut_sentences(buf, min_chars, flush=False):
    """Split off whole sentences worth speaking, and return (chunks, remainder).

    Chunks are held to a minimum length because each render costs ~0.3 s fixed: below roughly
    half a second of audio, generating the next chunk takes longer than playing the current
    one and the speech develops gaps. On flush, whatever is left goes out regardless."""
    # An unclosed code fence means more of it is still streaming in — wait rather than read
    # half a fence out loud.
    if not flush and buf.count("```") % 2:
        return [], buf
    chunks, start = [], 0
    for m in _BOUNDARY.finditer(buf):
        dot = m.start()          # just past the . ! or ?
        end = m.start(1)         # …and past any closing quote or bracket, which belongs to
                                 # this sentence, not to the gap between sentences
        if not _is_real_end(buf, dot):
            continue
        if end - start < min_chars:
            continue                                # too short: let it grow into the next one
        chunks.append(buf[start:end].strip())
        start = m.end()
    remainder = buf[start:]
    if flush:                       # flush means nothing is held back, whitespace included
        if remainder.strip():
            chunks.append(remainder.strip())
        remainder = ""
    return [c for c in chunks if c], remainder
