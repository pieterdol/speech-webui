"""Turning written text into something worth reading out loud, and cutting it into pieces
an engine can speak. Shared by chat, the studio and book narration."""
import re

# phonetically on the way in.
RESPELL = {"Pieter": "Peter"}


def respell(text):
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
