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
