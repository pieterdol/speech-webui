"""Turning written text into something worth reading out loud, and cutting it into pieces
an engine can speak. Shared by chat, the studio and book narration."""
import re

# Neither engine takes a pronunciation override, so hard words are respelled phonetically on
# the way in. Kokoro's documented [word](/phonemes/) form belongs to the KPipeline package and
# does nothing here: kokoro_onnx offers only all-or-nothing is_phonemes, so the markup is read
# out loud as "slash m stress u lengthen v i z slash". Respelling is the whole toolkit.
RESPELL = {
    "Pieter": "Peter",
    "movies": "movees",     # espeak clips the -ies to "movis"
}

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


ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def number_word(n):
    """A number as words. Kokoro reads a bare "21" acceptably, but "twenty-one" is unambiguous
    and doesn't risk being read as a year or a list item."""
    if n < 0 or n > 999:
        return str(n)
    if n < 20:
        return ONES[n] or "zero"
    if n < 100:
        return TENS[n // 10] + (f"-{ONES[n % 10]}" if n % 10 else "")
    rest = n % 100
    return ONES[n // 100] + " hundred" + (f" {number_word(rest)}" if rest else "")

def year_word(n):
    """A four-digit year the way it's said — "nineteen sixty-three", not "one thousand nine
    hundred and sixty-three", which is what espeak makes of the digits. Years mostly go in
    pairs; the round thousand, the noughties and the round hundreds are the three that don't."""
    if not 1000 <= n <= 9999:
        return number_word(n)
    if n % 1000 == 0:
        return f"{number_word(n // 1000)} thousand"                     # 2000
    if n % 1000 < 10:
        return f"{number_word(n // 1000)} thousand {number_word(n % 1000)}"   # 2005
    if n % 100 == 0:
        return f"{number_word(n // 100)} hundred"                       # 1900
    return f"{number_word(n // 100)} {number_word(n % 100)}"            # 1963, 2010


# Dates and pairs written with a slash. Read as written, "11/22/63" comes out as "eleven slash
# twenty-two slash sixty-three": the slash is a writing convention, not a sound. Saying each
# group as a number is what a person does with these — "eleven, twenty-two, sixty-three",
# "nine, eleven", "twenty, twenty" — and it needs no guess about which group is the month,
# which a spoken-out date would ("10/7" is October 7th in the American books here and July 10th
# in a Dutch one).
#
# The comma in place of the slash is doing real work: it's the beat between the groups. Joined
# by a space they run together into one long number, which is the other way to get this wrong.
#
# Groups are numbers only, so "and/or" and "Jake/George" are never touched, and a slash on
# either side rules out a URL or a path.
_SLASH_NUMBERS = re.compile(r"(?<![\w/.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\w/])")

def _spoken_slash(match):
    groups = [g for g in match.groups() if g]
    # Two single digits are a fraction far more often than a date, and "one two" is worse than
    # the slash. Three groups are a date whatever their sizes.
    if len(groups) == 2 and all(len(g) == 1 for g in groups):
        return match.group(0)
    say = year_word if len(groups[-1]) == 4 else number_word
    return ", ".join([number_word(int(g)) for g in groups[:-1]] + [say(int(groups[-1]))])


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


def respell(text):
    for pattern, full in _HONORIFIC:
        text = pattern.sub(full, text)
    for pattern, full in _SPOKEN:
        text = pattern.sub(lambda m, f=full: _expand(m, f), text)
    text = _NUMBER_OF.sub("number", text)
    text = _SLASH_NUMBERS.sub(_spoken_slash, text)
    for src, dst in RESPELL.items():
        text = re.sub(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
    return text


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
