"""Who speaks each line of a chapter, so one book can be read by more than one voice.

The work is split between code and model on purpose. The quoted runs are found here, so the
model is never asked to hand text back: asked to quote a character's first line it gets it
wrong about half the time, while asked "who says number 14" about runs that are already marked
in the text it is right nearly always. Where the chapter names the speaker outright — "said
Bingley" — code takes the answer and the model doesn't get a vote.

What comes out is one speaker per quoted run, a distinct voice per speaker, and everything
between the quotes left to the narrator.
"""
import json
import re
import time
import urllib.request

from chat import OLLAMA

CAST_MODEL = "qwen3:14b"
# Shorter than chat's, and for the same reason: a resident 14b holds 9.3 GB of the card's 16, and
# nothing is going to ask it anything else between chapters.
CAST_KEEP_ALIVE = "2m"
# 8b gets the easy chapters right and drifts through unattributed exchanges — the very place a
# voice switch has to be right. 14b costs about the same wall-clock here because it spends fewer
# tokens getting there, so it's the default.
CAST_NUM_CTX = 16384
# One window of chapter at a time. A chapter can be 108,000 characters in this library, which no
# useful context holds along with an answer per quote, and accuracy falls off long before the
# context does. Windows break on line boundaries so no quoted run is cut in half.
CAST_WINDOW_CHARS = 24000
# How many already-attributed runs the next window is told about. A window that opens mid-scene
# has no other way to know who has been talking.
CAST_CARRY = 6
CAST_TIMEOUT = 1800

# Not speech, and unknown speaker: both read by the narrator, and kept apart because they mean
# different things to anyone reading the result — a caption is meant to be narrated, an
# unattributed line is a gap in the attribution.
NOT_SPEECH = "none"
UNKNOWN = "unknown"


# ---- finding the quoted runs ----

# Curly first, because that's what nearly every EPUB in this library uses; straight quotes are
# the fallback for the few that don't. Only doubles — a single quote is an apostrophe far more
# often than it is speech.
QUOTE_PAIRS = (("“", "”"), ('"', '"'))


def quote_style(text):
    """Which pair of marks this text quotes speech with."""
    for pair in QUOTE_PAIRS:
        if pair[0] in text:
            return pair
    return QUOTE_PAIRS[0]


def quote_spans(text):
    """(start, end) for every run of quoted speech in `text`, in order.

    A run ends at its closing mark or at the end of its line, whichever comes first. Speech that
    carries across a paragraph break is printed with an opening mark on every paragraph and a
    closing one only at the end, so a run that always waited for the closing mark would swallow
    the narration in between — and plenty of chapter files have an unbalanced count of marks
    anyway, where one run would then run to the end of the chapter. Stopping at the line also
    keeps every run inside one segment, which is what lets rendering match them up again.
    """
    open_mark, close_mark = quote_style(text)
    spans, i = [], 0
    while True:
        start = text.find(open_mark, i)
        if start < 0:
            return spans
        end = text.find(close_mark, start + len(open_mark))
        line_end = text.find("\n", start)
        if end < 0 or (0 <= line_end < end):
            end = line_end if line_end >= 0 else len(text)
        else:
            end += len(close_mark)
        if end > start:
            spans.append((start, end))
        i = max(end, start + 1)


def marked(text, spans):
    """`text` with [1] [2] … in front of each run, which is what the model is asked about.

    Markers rather than a list of the runs on their own: who is speaking is in the narration
    around them, so the model needs the chapter, and it needs to be able to point at a run
    without writing it out.
    """
    out, at = [], 0
    for n, (start, end) in enumerate(spans, 1):
        out.append(text[at:start])
        out.append(f"[{n}]")
        out.append(text[start:end])
        at = end
    out.append(text[at:])
    return "".join(out)


# ---- what the chapter says outright ----

# The verbs a chapter attributes speech with. Only ones that can carry a speaker directly, so
# "he laughed" is out — it says nothing about who "he" is, and the model is better placed to
# answer that anyway.
TAG_VERBS = ("said", "says", "asked", "asks", "answered", "answers", "replied", "replies",
             "told", "tells", "shouted", "shouts", "whispered", "whispers", "murmured",
             "murmurs", "muttered", "mutters", "cried", "cries", "called", "calls", "adds",
             "added", "continued", "continues", "repeated", "repeats", "explained", "explains",
             "announced", "announces", "demanded", "demands", "insisted", "insists", "agreed",
             "agrees", "admitted", "admits", "warned", "warns", "observed", "observes",
             "remarked", "remarks", "offered", "offers", "yelled", "yells", "begged", "begs")
_VERBS = "|".join(TAG_VERBS)
# A name as a chapter writes one, optionally behind a title. Two words at most: past that it
# stops being a name and starts being a clause.
_TITLE = r"(?:Mr|Mrs|Ms|Miss|Dr|Sir|Lady|Lord|Professor|Prof|Captain|Aunt|Uncle|Father|Mother)"
_NAME = rf"(?:{_TITLE}\.?\s+)?[A-Z][\w’']*(?:\s+[A-Z][\w’']*)?"
# Both orders a tag comes in — "said Bingley", "Daniela says" — read from just after the run that
# closed. Anchored at the run's end and allowed only punctuation and space before the tag, so a
# name further along the sentence — a person being spoken *about* — can't be read as the speaker.
_VERB_FIRST = re.compile(rf"\A[\s,.;:!?—–-]*(?:{_VERBS})\s+({_NAME})")
_NAME_FIRST = re.compile(rf"\A[\s,.;:!?—–-]*({_NAME})\s+(?:{_VERBS})\b")
# The same two shapes with a pronoun in place of the name. They say nothing about who is speaking
# — that's the model's job — but they settle what the model is worst at: a masked man nobody has
# named is still "he", and a voice is all a listener needs.
_HE_SHE = re.compile(rf"\A[\s,.;:!?—–-]*(?:(?:{_VERBS})\s+(he|she)\b|(he|she)\s+(?:{_VERBS})\b)")


def tagged_speaker(text, span, names):
    """The speaker the chapter names right after a run, when it's someone we know about.

    Restricted to `names` — the speakers the model found — because a tag reads "said the man in
    the mask" as often as it reads "said Amanda", and a cast member called "The" is worse than
    no answer. What it buys is the case the model gets wrong most: which of two people in a
    scene said this one, where the chapter has already said so in as many words.
    """
    after = text[span[1]:span[1] + 60]
    for pattern in (_VERB_FIRST, _NAME_FIRST):
        m = pattern.match(after)
        if not m:
            continue
        found = m.group(1).strip().rstrip(".,;:")
        for name in names:
            # A tag prints as much of the name as it needs to: "said Leighton" for Leighton
            # Vance, "said Mr. Bennet" where the cast has Mr. Bennet.
            if found == name or found in name.split() or name in found.split():
                return name
    return None


def tagged_gender(text, span):
    """"male" or "female" from the pronoun in the tag after a run, or None.

    Worth having in code because the model reads the question as "is this person identified":
    ninety lines of a man in a mask came back "unknown", which costs him a voice and hands his
    half of an abduction to the narrator. The chapter says "he says" every time.
    """
    m = _HE_SHE.match(text[span[1]:span[1] + 60])
    if not m:
        return None
    return "male" if (m.group(1) or m.group(2)).lower() == "he" else "female"


# ---- one utterance, two runs ----

def utterance_groups(text, spans):
    """Indices of the runs that are one utterance interrupted by a speech tag, as tuples.

    "…,” said he, “…" is one person speaking, and the two runs have to end up in one voice —
    split between two speakers it sounds like an interruption that isn't there. The test is that
    the gap between the runs stays on one line and doesn't end a sentence: that's a tag holding
    a single utterance open. Where the gap does end a sentence the two runs are separate
    utterances, which may well be two people ("“Yes,” said Jason. “No,” said Amanda."), and the
    model answers each on its own.
    """
    groups, current = [], [0] if spans else []
    for i in range(1, len(spans)):
        gap = text[spans[i - 1][1]:spans[i][0]]
        if "\n" in gap or len(gap) > 80 or re.search(r"[.!?…]", gap):
            groups.append(tuple(current))
            current = []
        current.append(i)
    if current:
        groups.append(tuple(current))
    return groups


# ---- names the model came back with ----

def clean_name(name):
    """A speaker's name with the model's stray punctuation off it."""
    return re.sub(r"\s+", " ", (name or "").strip()).strip(",.;:—-").strip()


def merge_names(names):
    """{name as answered: name to use}, folding a short answer into the full one.

    A model naming the same person "Leighton" in one line and "Leighton Vance" in the next would
    otherwise cast them twice, in two voices, in one scene. Folded only where the short form
    belongs to exactly one longer name — "Bennet" with both Mr. and Mrs. Bennet in the chapter
    is genuinely ambiguous and stays as it is.
    """
    keep = {}
    full = [n for n in names if " " in n]
    for name in names:
        if " " in name or name.casefold() in (NOT_SPEECH, UNKNOWN):
            keep[name] = name
            continue
        owners = [f for f in full if name in f.split()]
        keep[name] = owners[0] if len(owners) == 1 else name
    return keep


def speakers_in(lines):
    """The cast: one entry per speaker, in the order they first speak.

    [{name, gender, lines}] — `lines` being how many runs they get, which is what makes the
    difference between a character worth a voice of their own and a passer-by with one line.
    """
    order, seen = [], {}
    for line in lines:
        name = line.get("speaker") or UNKNOWN
        if name.casefold() in (NOT_SPEECH, UNKNOWN):
            continue
        if name not in seen:
            seen[name] = {"name": name, "gender": line.get("gender") or UNKNOWN, "lines": 0,
                          "votes": {}}
            order.append(seen[name])
        e = seen[name]
        e["lines"] += 1
        g = line.get("gender") or UNKNOWN
        e["votes"][g] = e["votes"].get(g, 0) + 1
    out = []
    for e in order:
        # A speaker's gender is what most of their lines say it is: it decides which voice they
        # get, and one line answered "unknown" shouldn't cost them a voice.
        votes = {g: n for g, n in e["votes"].items() if g != UNKNOWN} or e["votes"]
        out.append({"name": e["name"], "lines": e["lines"],
                    "gender": max(votes, key=lambda g: (votes[g], g != UNKNOWN))})
    return out


# ---- asking the model ----

SYSTEM = """You attribute quoted speech in a book chapter so each speaker can be given a
different synthetic voice.

Every quoted run in the chapter is marked [1], [2], … just before it. For each marker, say who
speaks that run.

Rules:
- Use the same name for a person every time, the fullest one the chapter gives.
- A single speech split by "said X" is two markers with the same speaker.
- Some marked runs are not speech: a caption, a title, a sign, a quoted phrase. Answer "none"
  for those.
- Answer "unknown" only when the chapter genuinely does not say who speaks.
- Gender is how the chapter refers to them — he, she, the man, the woman, a title — and not
  whether they have been identified: a man in a mask whose name nobody knows is still male. It
  decides which voice reads them, so "unknown" is a last resort.
- Cover every marker, in order, exactly once."""

SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "gender": {"type": "string", "enum": ["male", "female", "unknown"]},
                },
                "required": ["n", "speaker", "gender"],
            },
        },
    },
    "required": ["quotes"],
}


def windows(text, spans, limit=CAST_WINDOW_CHARS):
    """The chapter cut into pieces small enough to ask about, as (text, runs before it, runs in
    it).

    Cut on line boundaries: a run is never split, and every window is whole paragraphs, so the
    narration that says who is speaking stays with the speech it belongs to.
    """
    out, start, before = [], 0, 0
    while start < len(text):
        if len(text) - start <= limit:
            end = len(text)
        else:
            end = text.rfind("\n", start + 1, start + limit) + 1
            if end <= start:                      # one line longer than a whole window
                end = start + limit
        here = sum(1 for s, _e in spans if start <= s < end)
        out.append((text[start:end], before, here))
        before += here
        start = end
    return out or [(text, 0, 0)]


def ask(window, model=CAST_MODEL, carry=(), url=None, timeout=CAST_TIMEOUT):
    """The model's answers for one window: [{n, speaker, gender}], n counting from 1.

    `carry` is the last few (speaker, run) pairs from the window before, so a window that opens
    mid-conversation knows who has been talking.
    """
    lead = ""
    if carry:
        lead = ("Just before this, in order:\n"
                + "\n".join(f"{who}: {what}" for who, what in carry) + "\n\n")
    body = {"model": model, "stream": False, "format": SCHEMA,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": lead + "Chapter:\n\n" + window}],
            "keep_alive": CAST_KEEP_ALIVE,
            # Temperature 0: the same chapter has to come back the same way, or re-running the pass
            # would re-cast the book. num_ctx explicitly, because Ollama's default would quietly
            # truncate a window and the runs it dropped would come back unanswered.
            "options": {"temperature": 0, "num_ctx": CAST_NUM_CTX}}
    req = urllib.request.Request((url or OLLAMA) + "/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    answers = json.loads(out["message"]["content"]).get("quotes") or []
    return [a for a in answers if isinstance(a.get("n"), int)]


def collate(answers, count):
    """The model's answers as one speaker per run, in run order.

    An answer per marker is what the schema asks for and not what always comes back: a model can
    stop early on a long list, or answer a marker twice. Missing runs are `unknown`, which the
    narrator reads — a gap in the attribution has to be visible, not filled with whoever spoke
    last.
    """
    got = {}
    for a in answers:
        got.setdefault(a["n"], a)
    out = []
    for n in range(1, count + 1):
        a = got.get(n) or {}
        out.append({"n": n, "speaker": clean_name(a.get("speaker")) or UNKNOWN,
                    "gender": a.get("gender") or UNKNOWN})
    return out


def attribute(text, model=CAST_MODEL, url=None, ask_fn=None):
    """Who speaks every quoted run in a chapter.

    Returns {model, made, quotes, lines, speakers, tagged}: `lines` is one entry per run in order
    with the speaker and where the answer came from, `speakers` is the cast, and `tagged` counts
    how many of the answers code settled rather than the model.
    """
    asker = ask_fn or ask
    spans = quote_spans(text)
    answers, carry = [], []
    for window, before, here in windows(text, spans):
        # Marked with the window's own numbering, from one: what comes back is per marker, and a
        # window is all the model ever sees, so numbering it chapter-wide would only give it four
        # hundred to count through.
        ask_about = marked(window, quote_spans(window))
        got = collate(asker(ask_about, model=model, carry=carry, url=url), here)
        # Window-local numbering to chapter-wide, so the caller only ever sees run numbers.
        answers += [dict(a, n=a["n"] + before) for a in got]
        # What the next window is told: the last few runs of this one and who said them.
        carry = [(a["speaker"], text[spans[a["n"] - 1][0]:spans[a["n"] - 1][1]][:120])
                 for a in answers[-CAST_CARRY:]]
    lines = collate(answers, len(spans))
    names = {clean_name(l["speaker"]) for l in lines} - {NOT_SPEECH, UNKNOWN, ""}
    keep = merge_names(sorted(names))
    for line in lines:
        line["speaker"] = keep.get(line["speaker"], line["speaker"])
        line["how"] = "model"
    # Now the two things code decides. The tag first, so a named speaker wins over the model's
    # guess; then the grouping, so an utterance split by that tag comes out in one voice.
    known = sorted({l["speaker"] for l in lines} - {NOT_SPEECH, UNKNOWN, ""})
    for i, (start, end) in enumerate(spans):
        tagged = tagged_speaker(text, (start, end), known)
        if tagged and tagged != lines[i]["speaker"]:
            lines[i].update(speaker=tagged, how="tag")
        elif tagged:
            lines[i]["how"] = "tag"
        # Only where the chapter says it outright: a pronoun in the tag beats an answer of
        # "unknown", and beats a wrong guess too.
        pronoun = tagged_gender(text, (start, end))
        if pronoun:
            lines[i]["gender"] = pronoun
    for group in utterance_groups(text, spans):
        # The one the chapter names, else the first answer that names anyone.
        lead = next((i for i in group if lines[i]["how"] == "tag"), None)
        if lead is None:
            lead = next((i for i in group
                         if lines[i]["speaker"] not in (NOT_SPEECH, UNKNOWN)), None)
        if lead is None:
            continue
        for i in group:
            if i != lead and lines[i]["speaker"] != lines[lead]["speaker"]:
                lines[i].update(speaker=lines[lead]["speaker"], gender=lines[lead]["gender"],
                                how="split")
    return {"model": model, "made": int(time.time()), "quotes": len(spans), "lines": lines,
            "speakers": speakers_in(lines),
            "tagged": sum(1 for l in lines if l["how"] == "tag")}


# ---- a voice each ----

# Kokoro names a voice for its accent and gender: af_heart is American female, bm_george British
# male. That's the whole roster this needs — a character is cast from the narrator's own accent,
# so a chapter doesn't wander between continents.
_KOKORO = re.compile(r"^([a-z])([fm])_")


def voice_pool(narrator, roster):
    """{"female": [...], "male": [...]} — the voices a book's characters can be cast from.

    The narrator's accent, never the narrator's own voice. A Piper voice has no such naming and
    Dutch has one or two voices installed at all, so a Dutch book comes back with an empty pool
    and stays single-voiced. That's a real limit of the engines here, not something to work
    around: two characters sharing a voice is worse than one narrator reading both.
    """
    m = _KOKORO.match(narrator or "")
    pool = {"female": [], "male": []}
    if not m:
        return pool
    for v in roster:
        vm = _KOKORO.match(v)
        if vm and vm.group(1) == m.group(1) and v != narrator:
            pool["female" if vm.group(2) == "f" else "male"].append(v)
    return pool


def assign_voices(speakers, narrator, roster, taken=None):
    """{speaker: voice}, adding to `taken` rather than replacing it.

    Additive because the map outlives the chapter: attributing chapter two must not re-cast the
    people you have already heard in chapter one. Given in order of how much they speak, so when
    the voices run out it's the passers-by that go without.

    A speaker whose gender the chapter never showed keeps the narrator's voice: guessing gets it
    wrong half the time, and hearing a man read in a woman's voice is worse than hearing the
    narrator read his line.
    """
    voices = dict(taken or {})
    pool = voice_pool(narrator, roster)
    used = set(voices.values()) | {narrator}
    for s in sorted(speakers, key=lambda s: (-s.get("lines", 0), s["name"])):
        if s["name"] in voices or s.get("gender") not in pool:
            continue
        free = [v for v in pool[s["gender"]] if v not in used]
        if not free:
            continue
        voices[s["name"]] = free[0]
        used.add(free[0])
    return voices


# ---- reading it back out ----

def voiced_runs(text, lines, at, narrator, voices):
    """(runs, next index): `text` as [(piece, voice)] with each quoted run in its speaker's
    voice, and the narration between them in the narrator's.

    `lines` is the chapter's attribution and `at` how far into it this piece of the chapter
    starts, because a chapter is rendered a segment at a time while the attribution is one list
    for the whole chapter. Running past the end of that list raises: an attribution made before
    the chapter was re-scanned would otherwise shift every voice after the change by one, which
    sounds like a bug in the cast rather than the stale file it is. The caller checks the count
    up front for the same reason.
    """
    runs, spans = [], quote_spans(text)
    prev = 0
    for i, (start, end) in enumerate(spans):
        line = lines[at + i] if at + i < len(lines) else None
        if line is None:
            raise ValueError(f"attribution has {len(lines)} runs, chapter has more")
        if start > prev:
            runs.append((text[prev:start], narrator))
        runs.append((text[start:end], voices.get(line["speaker"]) or narrator))
        prev = end
    if prev < len(text):
        runs.append((text[prev:], narrator))
    return [(t, v) for t, v in runs if t.strip()], at + len(spans)
