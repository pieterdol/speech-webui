"""EPUB narration: the book index, rendering chapters to opus, and the .m4b export.

Owns its own storage paths rather than taking them from core, which is what lets the tests
point the whole layer at a tmpdir.
"""
import hashlib, html, json, os, re, shutil, subprocess, tempfile, threading, time
import urllib.parse, uuid

from flask import Response, jsonify, request, send_from_directory

import cast
import epub
import openlib
from core import (app, index_lock, jobs, log, log_transfer, new_job, run_lock, safe_path,
                  write_json, HERE)
from media import audio_seconds, pad_with_silence
from textprep import (ONES, TENS, clean_respell, cut_sentences, respell,
                      respell_diff, respell_pattern, spoken_initials)
from tts import kokoro_voices, piper_voice_ids, tts_engine_of, tts_say

BOOKS_DIR  = os.path.join(HERE, "books")
BOOKS_FILE = os.path.join(HERE, "books.json")
os.makedirs(BOOKS_DIR, exist_ok=True)

# ---- books (books.json + books/<id>/) ----
# A book is a lot of text and a lot of audio, so books.json holds only the index: chapter
# names, word counts, which segments have been rendered, and where you'd got to. The prose
# lives in books/<id>/text/ and the audio in books/<id>/audio/.
#
# Rendering is per chapter, on demand, because the whole of The Institute is 8.4 hours of
# work — you'd never wait for that. Chapters are cut into ~10 minute segments so the first
# audio arrives in a few minutes rather than sixteen, which is safe now that iOS is confirmed
# to advance between files with the screen locked.
SEGMENT_CHARS = 8000      # ≈10 min of speech at the measured ~13.6 characters per second
# How many chapters a pronunciation change may re-narrate before it asks. Two is a name
# in a couple of places; twenty is a word common enough to be a mistake.
RESPELL_CONFIRM_AT = 2
CHUNK_CHARS   = 600       # one Kokoro/Piper call ≈45 s of audio ≈17 s of work

# ---- the render queue ----
# One list, in order, and one worker draining it. Chapters used to be a thread each, blocked on a
# lock: the order was whatever Python handed out, a hundred queued chapters were a hundred blocked
# threads, and nothing could be taken off — a thread waiting on a lock can't be interrupted, which
# is the point of a lock. An explicit list is cancellable, and it says what will happen next.
#
# Entries are (book_id, chapter index). `current` is the one being narrated, and is not in the
# list. `tokens` is one per queued job, so whoever asked can wait for it and be told if it was
# taken off — the whole-book run needs both.
render_queue      = []
render_state      = {"current": None, "tokens": {}, "worker": None}
render_state_lock = threading.Lock()

def load_books():
    try:
        with open(BOOKS_FILE) as f: return json.load(f)
    except Exception:
        return []

def write_books(items):
    write_json(BOOKS_FILE, items)

def find_book(book_id):
    return next((b for b in load_books() if b.get("id") == book_id), None)

def book_dir(book_id, *parts):
    return os.path.join(BOOKS_DIR, book_id, *parts)

# ---- where a chapter's files live ----
# A chapter's number *is* its position in book["chapters"] — `i`, which the page, the saved
# position, every count and about forty-five places on the server all assume. That was also its
# filename, and the two can't both be true once a section can be put back into the middle of the
# book: inserting at 1 in a book of 192 would mean renaming 191 text files and some 400 opus
# files, rewriting the filenames copied into every chapter's `segments`, and having a rollback
# path for a half-renamed directory.
#
# So storage keeps the number a chapter was created with and the list is free to renumber. `key`
# is that number; absent means the two have never diverged, which is every book nothing has been
# inserted into. Nothing else about a key means anything — it is not an order and not an id to
# show anyone, only the name its files are under.

def chapter_key(chapter):
    return chapter.get("key", chapter["i"])

def key_at(book, index):
    """The storage number of the chapter at a position, for the callers that have an index and
    the book but not the chapter. Out of range answers the index, which keeps the paths of a
    chapter that isn't there pointing at files that aren't there either."""
    chapters = (book or {}).get("chapters") or []
    return chapter_key(chapters[index]) if 0 <= index < len(chapters) else index

def text_file(book_id, key):
    return book_dir(book_id, "text", f"ch{key:03d}.txt")

def audio_file(book_id, key, si):
    return book_dir(book_id, "audio", audio_name(key, si))

def audio_name(key, si):
    """What one part is called. Stored in the chapter's `segments` and asked for by the page, so
    it survives a renumbering the way the files themselves do."""
    return f"ch{key:03d}-s{si:02d}.opus"

def cast_file(book_id, key):
    """Who speaks each quoted run of one chapter. Under the chapter's storage number like its
    text and its audio, and on disk rather than in books.json for the same reason the prose is:
    a chapter has a couple of hundred quoted runs, and the index is rewritten after every
    segment of every render."""
    return book_dir(book_id, "cast", f"ch{key:03d}.json")

def load_attribution(book_id, key):
    try:
        with open(cast_file(book_id, key)) as f:
            return json.load(f)
    except Exception:
        return None            # never attributed, or unreadable: read it in one voice

def write_attribution(book_id, key, data):
    os.makedirs(book_dir(book_id, "cast"), exist_ok=True)
    write_json(cast_file(book_id, key), data)

def renumber(chapters):
    """Make every chapter's `i` its position again, in place, keeping its storage number.

    `key` is dropped where the two agree, so a book nothing has been put back into carries no
    trace of any of this and reads exactly as it did before."""
    for i, c in enumerate(chapters):
        key = chapter_key(c)
        c["i"] = i
        if key == i:
            c.pop("key", None)
        else:
            c["key"] = key
    return chapters

def book_summary(b):
    """Enough for the library list without shipping every chapter. Counted over what will be
    narrated, so a book left out of nothing reads the same as before and one with apparatus
    marked off doesn't sit at "15 of 16" for good."""
    chapters = chapters_in(b)
    ready = sum(1 for c in chapters if c.get("state") == "ready")
    return {k: b.get(k) for k in ("id", "title", "author", "language", "voice",
                                  "added", "position", "cover")} | \
           {"chapters": len(chapters), "ready": ready,
            "cover_v": cover_version(b.get("id")),
            "words": sum(c.get("words", 0) for c in chapters)}

def skipped_index(skipped):
    """What the index keeps about a left-out section: what it was, how long, and why it went.

    Not its text. epub.extract hands the prose over so a section can be read back or put in as a
    chapter — see api_book_skipped and api_book_insert — but books.json is an index, rewritten
    after every chapter of every render, and twenty sections of prose in it would be paid for on
    every write. `at` is where the section would sit if it were kept, which is what makes putting
    one back a single tap; a book added before it existed simply hasn't got it, and the EPUB is
    re-read anyway before anything is put back."""
    return [{k: s[k] for k in ("name", "words", "why", "at") if k in s} for s in skipped[:40]]

def safe_name(text, fallback):
    """A title as a filename: no separators, no punctuation a player would choke on."""
    return re.sub(r"[^\w\- ]+", "", text or "").strip()[:80] or fallback

def epub_name(book):
    """What the book's EPUB is called when you take a copy. On disk every book's is book.epub;
    the name worth having on a phone is the book's own."""
    return safe_name(book.get("title"), "book") + ".epub"

def book_file(book_id, name):
    """A file of this book the page offers to take away, resolved from the name it offered: one
    of its exports, or the EPUB the narration was made from.

    One resolver because both are served by the same pair of routes — the file, and the page
    wrapped around it for iOS — and two of them would eventually disagree about what exists."""
    book = find_book(book_id)
    if book and name == epub_name(book):
        src = book_dir(book_id, "book.epub")
        return src if os.path.isfile(src) else None
    return safe_path(book_dir(book_id, "export"), name)

def book_epub(book):
    """What the page needs to offer the EPUB, or None for a book whose file has gone."""
    name = epub_name(book)
    src = book_dir(book["id"], "book.epub")
    if not os.path.isfile(src):
        return None
    return {"file": name, "bytes": os.path.getsize(src),
            "url": f"/export/{book['id']}/{urllib.parse.quote(name)}"}

def export_note_path(book_id, name):
    return book_dir(book_id, "export", name + ".json")

def write_export_note(book_id, name, text, seconds):
    """What the export came to, beside the file it describes.

    How many chapters went in, how many were unfinished or not narrated, and how long it plays:
    all of it was in the job's result and nowhere else, so it vanished on the next reload while
    the file it described stayed. Reading it back off the file would mean ffprobe per export per
    poll; a few bytes of JSON written once doesn't."""
    try:
        write_json(export_note_path(book_id, name), {"text": text, "seconds": seconds})
    except OSError:
        pass                 # the export itself is fine; it just won't say as much about itself

def book_exports(book_id):
    """The .m4b files already built for this book, newest first.

    An export is a file that stays on disk, but the link to it only existed in the panel the
    export job wrote — so reloading the page, or picking the phone up the next day, meant
    encoding the whole book again to get at a copy that was already there.

    Only finished ones: an export being encoded is called .m4b.part until it's whole, so it
    can't be listed, shared or deleted halfway through."""
    d = book_dir(book_id, "export")
    # An export is a snapshot and nothing rewrites it, so one built before a pronunciation
    # changed still says the old name. Not deleted — rebuilding is two hours of ffmpeg and the
    # copy on a phone is fine — but said out loud, so it isn't shared again by mistake.
    changed = ((find_book(book_id) or {}).get("respell_changed") or 0)
    found = []
    for name in os.listdir(d) if os.path.isdir(d) else []:
        path = os.path.join(d, name)
        if not name.endswith(".m4b") or not os.path.isfile(path):
            continue
        st = os.stat(path)
        note = {}
        try:
            with open(export_note_path(book_id, name)) as f:
                note = json.load(f)
        except (OSError, ValueError):
            pass             # exports built before the note existed simply say less
        found.append({"file": name, "bytes": st.st_size, "made": int(st.st_mtime),
                      "text": note.get("text"), "seconds": note.get("seconds"),
                      "stale": st.st_mtime < changed,
                      "url": f"/export/{book_id}/{urllib.parse.quote(name)}"})
    return sorted(found, key=lambda e: -e["made"])

def update_book(book_id, fn):
    """Read-modify-write one book under the index lock. Renders mutate chapter state from a
    worker thread while the page is reading, so this is never done in place."""
    with index_lock:
        items = load_books()
        for b in items:
            if b.get("id") == book_id:
                fn(b)
                b["updated"] = int(time.time())
        write_books(items)

# Two derivations of the cover, made once on upload. The original is often ~2 MB, which is
# wasteful to send a phone repeatedly. Both keep the book's own proportions: a phone shows
# cover art at whatever shape it's given — BookPlayer displays the tall artwork embedded in
# an exported .m4b full-height on the lock screen — so squaring one off only adds bars.
#
# thumb is for the library grid, the reader's header and the player, none wider than 104 px
# but all of them on a 3x screen. full is the lock screen and the .m4b's artwork, where iOS
# draws it about 1050 px across. min(…,iw) rather than a flat width so a book whose own cover
# is smaller than the target is left alone instead of being blown up: of three books here the
# source covers are 825, 986 and 1325 px wide.
COVER_SIZES = {
    "thumb": r"scale=min(400\,iw):-2",
    "full":  r"scale=min(1000\,iw):-2",
}

def cover_path(book_id, size):
    return book_dir(book_id, f"cover-{size}.jpg")

def cover_version(book_id):
    """What every /cover URL carries as ?v=, so replacing a cover shows up at once.

    The files are cached for a day and keep their names, so without this the library grid
    would go on drawing yesterday's image. Taken from the thumbnail's mtime rather than
    stored on the book: a cover can appear without the index changing at all, which is what
    ensure_cover does. Milliseconds because two replacements can land inside one second.
    """
    try:
        return int(os.path.getmtime(cover_path(book_id, "thumb")) * 1000)
    except OSError:
        return 0

def make_covers(book_id, raw):
    """raw = the original image bytes. Returns True if at least the thumbnail came out."""
    os.makedirs(book_dir(book_id), exist_ok=True)
    src = book_dir(book_id, "cover-src")
    with open(src, "wb") as f:
        f.write(raw)
    made = 0
    try:
        for size, vf in COVER_SIZES.items():
            r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", src, "-vf", vf,
                                "-q:v", "4", cover_path(book_id, size)],
                               capture_output=True, text=True, timeout=120)
            made += int(r.returncode == 0 and os.path.exists(cover_path(book_id, size)))
    finally:
        if os.path.exists(src): os.remove(src)
    return made > 0

def ensure_cover(book_id):
    """Covers are made on upload, but books added before that feature exists — or whose
    extraction failed — get one lazily from the stored EPUB rather than needing a re-add."""
    if os.path.exists(cover_path(book_id, "thumb")):
        return True
    src = book_dir(book_id, "book.epub")
    if not os.path.exists(src):
        return False
    try:
        raw = epub.cover(src)
    except Exception:
        return False
    return bool(raw) and make_covers(book_id, raw)

def split_segments(text, limit=SEGMENT_CHARS):
    """Cut a chapter into segment-sized pieces on sentence boundaries."""
    out, buf = [], ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 > limit and buf:
            out.append(buf.strip())
            buf = ""
        if len(para) > limit:
            # A single paragraph bigger than a whole segment: pack its sentences the way the
            # chunker does. Slicing it at the character limit first would cut whatever word
            # straddles the boundary in half and leave the halves in different segments.
            for sentence in cut_sentences(para, 0, flush=True)[0]:
                if len(buf) + len(sentence) + 1 > limit and buf:
                    out.append(buf.strip())
                    buf = ""
                buf = (buf + " " + sentence).strip()
        else:
            buf = (buf + "\n" + para).strip()
    if buf.strip():
        out.append(buf.strip())
    return out

def split_chunks(text, limit=CHUNK_CHARS):
    """Segment -> TTS-sized chunks, so run_lock is released between calls and a chat reply or
    a transcription can get in. Whole sentences only."""
    out, buf = [], ""
    for para in text.split("\n"):
        sentences, tail = cut_sentences(para.strip() + " ", 0, flush=True)
        for s in sentences:
            if len(buf) + len(s) + 1 > limit and buf:
                out.append(buf.strip())
                buf = ""
            buf = (buf + " " + s).strip()
    if buf.strip():
        out.append(buf.strip())
    return out

def chapter_segments(book, index):
    """The text of each of a chapter's segments, exactly as render_chapter would cut it — same
    file, same heading strip, same split — or [] when there's no text to read.

    Both the render and the repair have to agree about what segment 3 contains, so neither
    derives it privately."""
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        return []
    try:
        with open(text_file(book["id"], key_at(book, index))) as f:
            text = f.read()
    except OSError:
        return []               # render_chapter turns this into an error; nothing to repair
    return split_segments(chapter_text(book, index, text))

def chapter_text(book, index, text):
    """A chapter's prose with everything the announcement already says off the top of it: the
    chapter's own heading, and the book's title and author where a half-title page prints them
    above the first chapter."""
    chapters = book.get("chapters") or []
    name = chapters[index].get("name") or "" if 0 <= index < len(chapters) else ""
    by = BY.get((book.get("language") or "")[:2], "by")
    return epub.strip_heading(text, name, book.get("title") or "",
                              f"{by} {book['author']}" if book.get("author") else "")

FIND_FORMS = 12          # how many spellings one search answers with

def text_snippet(text, start, end, around=42):
    """A phrase from the book with the match in the middle of it, whitespace collapsed."""
    lo, hi = max(0, start - around), min(len(text), end + around)
    return ("…" if lo else "") + " ".join(text[lo:hi].split()) + ("…" if hi < len(text) else "")

def find_in_book(book, q, limit=FIND_FORMS):
    """The spellings of a word as the book actually prints them: each distinct form, how often it
    occurs, in how many chapters, and one phrase to see it in.

    Typing a respelling needs the *written* form exactly, which is the one thing a narrator
    saying it wrongly can't tell you — and hunting for it through the EPUB on a phone is worse
    than asking the text that's already on disk. Forms are runs of word characters, so searching
    "verme" answers "Vermeer" rather than "Vermeer's": that's what a respelling is keyed on, and
    it matches the possessive anyway.
    """
    q = (q or "").strip()
    needle = q.casefold()
    if len(needle) < 2:
        return []                     # one letter matches most of the book
    # Two searches, because they answer two different questions. A single word is looked for
    # *inside* words — "danie" finds "Daniela", which is the point when you can't spell it. A
    # phrase is looked for as written, through the same pattern a rule would use, so what comes
    # back is something you can respell: "Judges, Chapter 16" is a real key, and the whitespace
    # in it matches the line break the file happens to have there.
    phrase = re.search(r"\s", q) is not None
    hunt = respell_pattern(q) if phrase else None
    seen = {}
    for c in book.get("chapters") or []:
        try:
            with open(text_file(book["id"], chapter_key(c))) as f:
                text = f.read()
        except OSError:
            continue
        for m in hunt.finditer(text) if phrase else re.finditer(r"\w+", text):
            word = " ".join(m.group(0).split()) if phrase else m.group(0)
            if not phrase and needle not in word.casefold():
                continue
            e = seen.setdefault(word, {"word": word, "count": 0, "chapters": set(), "line": ""})
            e["count"] += 1
            e["chapters"].add(c["i"])
            if not e["line"]:
                e["line"] = text_snippet(text, m.start(), m.end())
    forms = sorted(seen.values(), key=lambda e: (-e["count"], e["word"]))[:limit]
    # A count rather than the list: a name can be in a hundred chapters, and what you're deciding
    # is only whether this is the spelling you meant.
    return [e | {"chapters": len(e["chapters"])} for e in forms]

def stale_segments(book, index, old, new):
    """Which of a chapter's segments the engine would now be given differently, as indices.

    The question is never "which segments contain the word" — it's whether what the engine gets
    changes. Asked that way, one comparison covers every case: a word added, a word edited, a
    word *removed* (the audio still says the respelled form), a book entry firing on some global
    rule's output, and an entry keyed "Doctor" reaching text that reads "Dr. Who". Compared per
    chunk, because chunks are what _render_segment actually feeds the engine.

    Segment 0 is also stale when the spoken lead-in has changed, which is how a respelling of
    the title, the author, or a part name is caught — without naming any of them here.

    A chapter with no recorded lead-in counts as stale in segment 0, exactly as render_chapter
    treats one: only a rendered chapter carries the record, so the answer is "can't tell" and
    the safe reading is "re-make it". For an un-narrated chapter that costs nothing — there's no
    file, so respell_repair_plan drops it.
    """
    hits = set()
    lang = book.get("language") or ""
    for si, seg in enumerate(chapter_segments(book, index)):
        if any(respell(c, old, lang) != respell(c, new, lang) for c in split_chunks(seg)):
            hits.add(si)
    chapters = book.get("chapters") or []
    if 0 <= index < len(chapters):
        spoken = [respell(p, new, lang) for p, _ in chapter_intro(book, index)]
        if chapters[index].get("intro") != spoken:
            hits.add(0)
    return hits

def chapter_cast(book, index, segments):
    """(attribution lines, {speaker: voice}) for a chapter about to be narrated, or (None, None).

    None the moment anything fails to line up: no attribution, nobody in it with a voice of their
    own, or a count of quoted runs that no longer matches the segments about to be spoken. One
    voice is the safe answer — it's what the book sounded like before this existed — where using
    an attribution the text has moved under would put every voice after the change on the wrong
    line, which sounds like the cast is broken rather than the file being stale.
    """
    voices = book.get("cast") or {}
    if not voices:
        return None, None
    lines = (load_attribution(book["id"], key_at(book, index)) or {}).get("lines") or []
    # Counted over the segments rather than the chapter text, because that is what rendering walks
    # and it can differ: a paragraph longer than a whole segment is packed by sentence, which can
    # fall inside a quotation.
    if not lines or len(lines) != sum(len(cast.quote_spans(s)) for s in segments):
        return None, None
    if not any(voices.get(l.get("speaker")) for l in lines):
        return None, None
    return lines, voices

def cast_applied(lines, voices):
    """{speaker: voice} for the people who actually get one in this chapter — which is what a
    render is answerable for, rather than the book's whole map.

    Reduced this far so that attributing chapter five doesn't throw away the render of chapter six
    that was running at the time: that adds names to the map without touching anybody already in
    it, and nobody in chapter six sounds different for it."""
    if not lines:
        return {}
    return {l["speaker"]: voices[l["speaker"]] for l in lines if (voices or {}).get(l["speaker"])}

def cast_would_apply(book, index):
    """The same, worked out from the book as it is now. Asked through chapter_segments so it can't
    disagree with the render about which attribution lines up and which doesn't — and asked at all
    only for a book that has a cast, since the answer for the rest is a text file read and split
    for nothing."""
    if not (book.get("cast") or {}):
        return {}
    lines, voices = chapter_cast(book, index, chapter_segments(book, index))
    return cast_applied(lines, voices)

def cast_changed(book, index, used):
    """Whether anyone who speaks in this chapter would now be read by a different voice."""
    return cast_would_apply(book, index) != (used or {})

def stragglers(book_id, index, used, cast_used=None):
    """Parts of this chapter, just made, that the book no longer agrees with.

    True when it found some, having deleted them and left the chapter pending for another pass.
    A render reads the book once and then holds the lock for the whole chapter — a quarter of an
    hour, or an hour on a long one — so anything saved meanwhile is missing from what it wrote.
    Rather than have every endpoint reach into a running render, the render checks itself on the
    way out, which makes one invariant of it: a chapter is only ready when what is on disk is
    what the book now says it should be.

    Three things can have moved: the pronunciation map, which affects any part, the opening
    announcement, which affects the first, and who reads which character. The first two are asked
    at once, because stale_segments already compares the recorded announcement against the one the
    book would produce now — and it is asked even when the map is untouched, which is how an
    opening note edited while the opening was still rendering used to slip through and be marked
    ready saying the old thing. A voice moving under a character takes the whole chapter: their
    lines are anywhere in it."""
    book = find_book(book_id)
    if not book:
        return False
    if cast_changed(book, index, cast_used):
        drop_chapter_audio(book_id, index)
        update_book(book_id, lambda b: b["chapters"][index].update(
            state="pending", error=None, seconds=None))
        return True
    plan = respell_repair_plan(book, used or {}, book.get("respell") or {})
    if index not in plan:
        return False
    apply_respell_repair(book_id, {index: plan[index]})
    return True

def respell_repair_plan(book, old, new):
    """{chapter index: [segment indices]} for the audio that a map change has made wrong.

    Only files that exist: a chapter yet to be narrated has nothing to repair, and a segment
    that was never made is simply rendered with the new map when its turn comes."""
    plan = {}
    for c in book.get("chapters") or []:
        i = c["i"]
        gone = sorted(si for si in stale_segments(book, i, old, new)
                      if os.path.exists(audio_file(book["id"], chapter_key(c), si)))
        if gone:
            plan[i] = gone
    return plan

def queue_render(book_id, index):
    """Put a chapter on the queue and make sure something is draining it.

    Returns that job's token — {done, dropped} — so a caller who has to know when the chapter is
    finished can wait on it, and can tell being narrated from being taken off the queue. Asking
    twice for a chapter already queued hands back the same token rather than queueing it again:
    two of the same job is work done twice, and the second one used to be a second blocked thread.

    A chapter already being narrated *is* queued again, on purpose. It's how a repair works — a
    pronunciation saved mid-render leaves that chapter pending on the way out, and the pass that
    fills the gap has to be allowed to ask for it. The panel shows one line for it either way.
    """
    job = (book_id, index)
    with render_state_lock:
        token = render_state["tokens"].get(job)
        if token is None:
            token = {"done": threading.Event(), "dropped": False}
            render_state["tokens"][job] = token
            render_queue.append(job)
        # Whether a worker exists is asked of the thread rather than kept in a flag, because a flag
        # left set by a worker that died — for any reason, including a bug in a render — wedges
        # everything queued behind it for the life of the process. `worker` is cleared by the worker
        # itself, under this lock, at the moment it decides the queue is empty: so a job appended
        # before that decision is seen by it, and one appended after finds nothing running and
        # starts a new one. There is no window where both are true.
        worker = render_state["worker"]
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=render_worker, daemon=True)
            render_state["worker"] = worker
            worker.start()
    return token

def render_worker():
    """Narrate whatever is on the queue, in order, then stop being.

    One at a time because the engine is one machine, and it ends when the queue is empty rather
    than idling for the life of the process — the next thing queued starts another. That also
    keeps "one book renders at a time" a fact about the shape of this rather than something a lock
    is asked to promise.
    """
    while True:
        with render_state_lock:
            if not render_queue:
                render_state["worker"] = None    # see queue_render: this is where it hands over
                return
            job = render_queue.pop(0)
            render_state["current"] = job
            token = render_state["tokens"].pop(job, None)
        try:
            render_chapter(*job)
        except Exception:
            # A render that throws has already recorded the error on its own chapter; what must
            # not happen is the queue behind it stopping with it.
            log.exception("narrating %s chapter %s fell over", *job)
        finally:
            with render_state_lock:
                if render_state["current"] == job:
                    render_state["current"] = None
            if token:
                token["done"].set()

def drop_from_queue(book_id, index):
    """Take a chapter off the queue, and say whether there was one to take off.

    Only one that is waiting. The chapter being narrated now isn't cancellable: stopping it
    part-way through a ten-minute part would leave a file nothing finishes, which is why a single
    chapter has never had a stop button.
    """
    job = (book_id, index)
    with render_state_lock:
        if job not in render_queue:
            return False
        render_queue.remove(job)
        token = render_state["tokens"].pop(job, None)
    if token:
        # Woken and told what happened, so a whole-book run waiting on this chapter steps over it
        # instead of asking for it again — which would put it straight back and make ✕ do nothing.
        token["dropped"] = True
        token["done"].set()
    return True

def render_cancelled(book_id, gen):
    """Whether this render should stop and throw away what it made — the narrator changed
    under it, or the book was deleted while it ran.

    Deleting used to slip through: the check was `(find_book(book_id) or {}).get("gen", 0)
    != gen`, which for a gone book compares 0 against the 0 a fresh book starts on and
    decides nothing has changed. The render carried on writing into a directory the delete
    had already removed, recreating it segment by segment, and every books.json update it
    made was a silent no-op — so it left orphaned audio for a book that no longer existed."""
    b = find_book(book_id)
    return b is None or b.get("gen", 0) != gen

def discard_render(book_id, index, audio_dir, made):
    """Throw away what a cancelled render produced. A narrator change puts the chapter back
    to pending; a deleted book takes the whole directory, since the render kept recreating it
    underneath the delete and there's nothing left to belong to."""
    for m in made:
        p = os.path.join(audio_dir, m["file"])
        if os.path.exists(p):
            os.remove(p)
    if find_book(book_id) is None:
        shutil.rmtree(book_dir(book_id), ignore_errors=True)
    else:
        update_book(book_id, lambda b: b["chapters"][index].update(
            state="pending", segments=[], error=None))

def render_status():
    """What the narrator is on and what is behind it. Composed from books.json each time
    rather than kept in step with it, so it can't drift from the chapters it describes."""
    with render_state_lock:
        current, waiting = render_state["current"], list(render_queue)
    books = {b["id"]: b for b in load_books()}

    def entry(job, state):
        bid, i = job
        b = books.get(bid) or {}
        chapters = b.get("chapters") or []
        c = chapters[i] if 0 <= i < len(chapters) else {}
        return {"book": bid, "title": b.get("title") or "", "chapter": i, "state": state,
                "name": c.get("name") or f"Chapter {i + 1}", "words": c.get("words") or 0,
                "done": len(c.get("segments") or []), "total": c.get("total") or 0}

    # A chapter can be queued while it is also the one being narrated — a repair asks for the
    # chapter a render is on, so that the pass after it fills the gap. One line for it either way.
    seen = set([current] if current else [])
    queue_ = []
    for j in waiting:
        if j not in seen:
            seen.add(j)
            queue_.append(entry(j, "waiting"))
    # A whole-book run doesn't queue its chapters up front — it takes the next pending one
    # each time round the loop — so the rest of it would otherwise be invisible.
    for b in books.values():
        ra = b.get("render_all") or {}
        if not ra.get("running"):
            continue
        for c in run_scope(b):
            if c.get("state") == "pending" and (b["id"], c["i"]) not in seen:
                queue_.append(entry((b["id"], c["i"]), "queued"))
    return {"current": entry(current, "narrating") if current else None, "queue": queue_}

def render_depth():
    """How many chapters are being narrated or waiting their turn right now.

    Not render_status()'s queue, which is longer on purpose — it projects the rest of a
    whole-book run so the page can list it. As an answer to "when does the chapter I just
    tapped start" that projection is nonsense: it would say 190 for a book with 190 chapters
    left, when a run only ever has the one chapter it is on actually queued.
    """
    with render_state_lock:
        return int(bool(render_state["current"])) + len(render_queue)

def busy_with(book_id):
    """Why this book can't be renumbered right now, or "" when it can.

    A render decides which chapter it is about after it takes the lock, which it may have been
    waiting an hour for — so one in flight or queued would survive a renumbering and then narrate
    whatever had moved into the position it was asked for. Nothing else is in the way: positions
    belong to one book, so another book narrating, or an export encoding, is no business of this.
    """
    book = find_book(book_id) or {}
    if (book.get("render_all") or {}).get("running"):
        return "a whole-book run is going — stop it first"
    if any(c.get("state") == "rendering" for c in book.get("chapters") or []):
        return "a chapter of this book is being narrated — it has to finish first"
    with render_state_lock:
        queued = ([render_state["current"]] if render_state["current"] else []) \
                 + list(render_queue)
    if any(b == book_id for b, _i in queued):
        return "a chapter of this book is waiting to be narrated — it has to finish first"
    return ""

def render_chapter(book_id, index):
    """Render one chapter to opus, a segment at a time. Marks progress in books.json as it
    goes so the page can show it.

    Narrates in the calling thread and does not queue: render_worker is what calls this, one job at
    a time, which is what makes "one book renders at a time" true. Anything that wants a chapter
    narrated asks queue_render for it.
    """
    book = find_book(book_id)
    if not book:
        return
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        return
    chapter = chapters[index]
    if chapter.get("state") == "ready" or chapter.get("skip"):
        return
    # A book that is being read by a cast keeps being read by one. Worked out here rather than
    # in the endpoints because every way a chapter gets narrated comes through this function —
    # a tap, a whole-book run, and the one that actually caught this: playing a chapter asks
    # for the next to be narrated, which read it in a single voice.
    #
    # Part of narrating the chapter rather than a job of its own, so it is one line in the queue
    # and the panel says this book is being narrated: it's a couple of minutes on top of twenty. A
    # model that isn't there is not a failure — the chapter is narrated in one voice, which is what
    # it would have been anyway.
    if cast_wanted(book, index):
        try:
            attribute_chapter(book_id, index)
        except Exception as e:
            log.info("cast: couldn't work out who speaks in %s chapter %s: %s",
                     book_id, index, e)
        book = find_book(book_id)
        chapters = (book or {}).get("chapters") or []
        if not (book and 0 <= index < len(chapters)):
            return                     # deleted while we were asking
        chapter = chapters[index]
        if chapter.get("skip"):
            return
    # Read now, with the rest of the chapter: a queued render can have waited an hour, and a
    # section put back in the meantime moves every later chapter's position without moving its
    # files. So which files this render is about is decided from the book
    # as it is now, not from the index the caller happened to hold.
    key = chapter_key(chapter)
    voice = book.get("voice") or "af_heart"
    # This book's own pronunciations, on top of the global map. Read here and passed down
    # rather than looked up inside respell(), because the same function serves the studio and
    # chat, where there is no book — and because the repair scan has to be able to ask what
    # this map would have produced.
    respellings = book.get("respell") or {}
    # And the book's language, for the one rule that turns on it: what a decimal point is
    # called. Passed down the same way and for the same reason.
    lang = book.get("language") or ""
    # Bumped whenever the narrator changes. A chapter that was already being rendered when
    # you switched would otherwise finish in the old voice and be marked ready, leaving one
    # chapter of the book in the wrong voice with nothing to show for it.
    gen = book.get("gen", 0)
    txt_path = text_file(book_id, key)
    try:
        with open(txt_path) as f:
            text = f.read()
    except OSError as e:
        update_book(book_id, lambda b: b["chapters"][index].update(
            state="error", error=f"missing text: {e}"[:200]))
        return
    # Whatever the lead-in is about to say comes off the top of the text, so nothing is read
    # out twice: the chapter's own heading, and the book's title and author where the page
    # above the first chapter prints them.
    text = chapter_text(book, index, text)
    intro = chapter_intro(book, index)

    # The lead-in lives in the chapter's first segment, and a resumed render keeps whatever
    # files are already on disk — so a chapter left half-made before the announcement
    # changed would keep an opening that no longer matches. Only the first one has to go.
    #
    # Recorded respelled, because respelled is what the engine is given: "11/22/63: A Novel"
    # goes in as "11, 22, 63: A Novel", and a change to how a phrase is pronounced leaves the
    # written form identical. Comparing what's written would call that opening current when
    # it no longer is.
    spoken = [respell(p, respellings, lang) for p, _ in intro]
    # Split before publishing the state, not after: how many parts a chapter comes to is
    # pure text work, and knowing it up front is the difference between "part 1 of 2" and
    # ten minutes of "starting…" in the queue panel.
    #
    # Split on the *written* text, never the respelled form. Respelling first would make the
    # segment count — and so every filename — depend on the pronunciation map, which would
    # mean changing one word invalidated the whole book by construction.
    segments = split_segments(text)
    # Who reads which line, for a chapter that has been attributed. Read here with the rest of
    # the book and passed down, so one render speaks one cast the whole way through: a
    # character given a different voice halfway would otherwise change voice halfway.
    lines, voices = chapter_cast(book, index, segments)
    cast_used = cast_applied(lines, voices)
    update_book(book_id, lambda b: b["chapters"][index].update(
        state="rendering", error=None, done=0, segments=[], intro=spoken,
        total=len(segments)))
    audio_dir = book_dir(book_id, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    if chapter.get("intro") != spoken:
        stale = audio_file(book_id, key, 0)
        if os.path.exists(stale):
            os.remove(stale)
    made = []
    at = 0                  # how far into the attribution the segments have got
    try:
        for si, seg_text in enumerate(segments):
            # Between segments, not only at the end: deleting a book or changing the
            # narrator used to leave the whole rest of the chapter still to render before
            # anything noticed, which on a long chapter is most of an hour.
            if render_cancelled(book_id, gen):
                break
            name = audio_name(key, si)
            out  = os.path.join(audio_dir, name)
            # Walked for every segment, including the ones already on disk: the run numbers
            # are the chapter's, so a resumed render has to count past what it is skipping.
            runs = None
            if lines:
                runs, at = cast.voiced_runs(seg_text, lines, at, voice, voices)
            if not os.path.exists(out):
                # the closing pause belongs to the chapter, so only the last part gets it
                _render_segment(seg_text, voice, out,
                                intro=intro if si == 0 else None,
                                tail_pause=CHAPTER_END_PAUSE if si == len(segments) - 1 else 0,
                                respellings=respellings, lang=lang, runs=runs)
            made.append({"file": name, "seconds": audio_seconds(out)})
            # publish each finished segment: you can start listening to segment 1 while
            # segment 2 is still being made
            update_book(book_id, lambda b, m=list(made), n=len(segments):
                        b["chapters"][index].update(segments=m, done=len(m), total=n))
        if not render_cancelled(book_id, gen):
            # The book can have changed while this ran — a render holds the lock for the
            # whole chapter, so a respelling saved during segment 2 of 8 leaves the six after
            # it made the old way, and an opening note saved during segment 1 leaves the
            # announcement saying the old thing. Checked here rather than in every endpoint
            # that can move either: a chapter is only ready when what's on disk is what the
            # book now says it should be. See stragglers.
            if stragglers(book_id, index, respellings, cast_used):
                return
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="ready", error=None,
                seconds=round(sum(s["seconds"] for s in made), 1)))
            return
    except Exception as e:
        # A book deleted mid-render takes its directory with it, so ffmpeg failing with
        # "No such file or directory" is the delete working, not a fault to report.
        if not render_cancelled(book_id, gen):
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="error", error=str(e)[:200]))
            return
    # Cancelled, however we got here: finished, broke out of the loop, or threw.
    discard_render(book_id, index, audio_dir, made)

PART_SEP = epub.PART_SEP     # how epub.py joins a part name to its chapter label

# Spoken lead-in before a chapter's text: "The Night Knocker" … "one" … the prose. The pause
# after each is real silence, not punctuation — a full stop buys about a third of a second,
# which isn't enough to read as "a new chapter is starting".
PART_PAUSE    = 1.2
CHAPTER_PAUSE = 0.9
# And the very top of the book gets its title and author, the way a published audiobook opens.
TITLE_PAUSE   = 0.7
AUTHOR_PAUSE  = 1.6
# An opening note — a dedication, a notice — read after the author. A beat between its sentences
# and a longer one before the book itself starts, so it doesn't run into chapter one.
CHUNK_PAUSE   = 0.5
OPENING_PAUSE = 1.8
OPENING_CHARS = 1000
# "by" in the book's own language. Everything else in the announcement is the book's own words;
# this one is ours, and read out by a Dutch voice the English word comes out as "bie".
BY = {"nl": "van"}
# And a longer one at the end, so a chapter closes rather than running straight into the next
# announcement — the moment you'd use to notice a chapter has ended.
CHAPTER_END_PAUSE = 1.8

# Digits to words lives in textprep, which is where everything about how text sounds lives.
# This is the reverse direction, for books that spell their chapter headings out: Dark Matter
# names them "Chapter One" rather than "Chapter 1", and looking only for digits found nothing —
# so the whole book was narrated with no announcement at all.
_ONES_N = {w: i for i, w in enumerate(ONES) if w}
_TENS_N = {w: i * 10 for i, w in enumerate(TENS) if w}
# The words a heading wraps its number in. Anything else in it makes it a title, and a title is
# read out as written. Only the languages the books are in are here; an unrecognised word for
# "chapter" costs nothing, since the heading is then announced whole — "Kapitel 19" rather than
# "19", which is what it says anyway.
NUMBER_WORDS = ("chapter", "part", "book", "and", "the", "hoofdstuk", "deel", "boek")
# The third way a book numbers its chapters, and the one the classics use: Pride and Prejudice
# writes "Chapter I.", A Tale of Two Cities "CHAPTER II. The Mail". Left as letters an engine
# reads "I" as the word and "IV" as two of them.
#
# The strict form only — "IIII" is not a numeral — and uppercase only, which is how a heading
# writes one. Both rules are there because the alternative is reading words as numbers.
_ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})")
_ROMAN_VALUE = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
# A numeral standing behind the word that says it's one, for a heading that carries a title as
# well and so is read out whole: "CHAPTER I. Down the Rabbit-Hole". Only there — a bare "I" in
# a title is the pronoun, and "I Am Legend" is not chapter one.
_ROMAN_AFTER_WORD = re.compile(r"(?i)\b(%s)\s+([IVXLCDM]+)\b" % "|".join(NUMBER_WORDS))
# …and a heading that opens with one, where the stop after it is what says it's a number rather
# than a word: The War of the Worlds has "II. The Falling Star". "I Am Legend" hasn't got one.
_ROMAN_AT_FRONT = re.compile(r"^([IVXLCDM]+)(?=[.:—–-]\s)")
# A footnote marker the heading brought with it off the page. Max Havelaar's first chapter is
# "EERSTE HOOFDSTUK[1]", and the engine reads the marker out as a number.
_FOOTNOTE_MARK = re.compile(r"\[\d+\]")

def word_number(label):
    """The chapter number written out in a heading, or None. Handles "One", "Twenty-One" and
    "One Hundred Twelve"; anything longer or stranger falls through to no announcement."""
    words = re.findall(r"[a-z]+", (label or "").lower())
    total, seen = 0, False
    i = 0
    while i < len(words):
        w = words[i]
        if w == "hundred" and seen:
            total *= 100
        elif w in _TENS_N:
            total += _TENS_N[w]
            seen = True
            # "twenty-one" arrives as two words either way, hyphen or space
            if i + 1 < len(words) and words[i + 1] in _ONES_N and _ONES_N[words[i + 1]] < 10:
                total += _ONES_N[words[i + 1]]
                i += 1
        elif w in _ONES_N:
            total += _ONES_N[w]
            seen = True
        elif w in NUMBER_WORDS:
            pass                                  # the words a heading wraps its number in
        else:
            return None                           # a titled section, not a numbered one
        i += 1
    return total if seen and 0 < total <= 999 else None

def roman_number(word):
    """The value of a roman numeral, or None.

    Capped at 999 like the spelled-out form, which also throws out the one English word written
    in nothing but numeral letters: "MIX" is a perfectly good 1009.
    """
    word = (word or "").strip().rstrip(".")
    if not word or not _ROMAN.fullmatch(word):
        return None
    total = 0
    for i, letter in enumerate(word):
        value = _ROMAN_VALUE[letter]
        after = _ROMAN_VALUE.get(word[i + 1]) if i + 1 < len(word) else 0
        total += -value if after and after > value else value
    return total if 0 < total <= 999 else None

def roman_label(label):
    """The number of a heading that is a roman numeral and nothing else — "Chapter I.", "II"."""
    words = [w for w in re.findall(r"[A-Za-z]+", label or "") if w.lower() not in NUMBER_WORDS]
    return roman_number(words[0]) if len(words) == 1 else None

def spoken_heading(label):
    """A heading as the announcement should say it: its roman numeral in digits, for the engine
    to read in whatever language it speaks, and without the footnote marker the page hung on
    it. Only what a heading picked up on its way here — the wording itself is the book's."""
    label = _FOOTNOTE_MARK.sub("", label or "").strip()
    label = _ROMAN_AT_FRONT.sub(lambda m: str(roman_number(m.group(1)) or m.group(1)), label)
    return _ROMAN_AFTER_WORD.sub(
        lambda m: f"{m.group(1)} {roman_number(m.group(2)) or m.group(2)}", label)

def label_number(label):
    """The number of a chapter from its heading, in digits or in words, or None.

    A heading can hold a number and still not be one: "Chapter 7: Overcoming Obstacles" and
    "AMPOULES REMAINING: 24" are titles, and announcing the number out of them would throw away
    what they actually say. So digits only count when the rest of the heading is the words a
    number gets wrapped in — the spelled-out form already works this way, since word_number
    gives up at the first word that isn't part of a number.
    """
    m = re.search(r"\d+", label or "")
    if not m:
        n = word_number(label)
        return n if n is not None else roman_label(label)
    rest = re.findall(r"[a-z]+", (label[:m.start()] + " " + label[m.end():]).lower())
    return int(m.group(0)) if all(w in NUMBER_WORDS for w in rest) else None

def title_label(label):
    """A chapter's heading when it's a title to read out, or "".

    A section with no heading and no entry in the table of contents is named after its own
    first words ("And Samson called unto the LORD,…") or, when it hasn't even got those, after
    the file it came from. Neither is a title: the first would read the opening of the chapter
    twice over, the second would say "fm00.html" out loud. Length is the same question
    strip_heading asks — past a line's worth it isn't a heading, it's prose.
    """
    label = (label or "").strip()
    if label.endswith(epub.OPENING_NAME) or re.fullmatch(r"[\w-]+\.x?html?", label, re.I):
        return ""
    return label if len(label) <= epub.HEADING_CHARS else ""

def spoken_title(book):
    """What the opening announcement calls the book. A title is written to be read, not heard:
    "11/22/63: A Novel" has a subtitle no narrator says out loud, and respell can only fix the
    parts of it that follow a rule. So the book carries an optional spoken form, and where it's
    empty the written title is already what you'd say."""
    return (book.get("spoken_title") or "").strip() or (book.get("title") or "")

def opening_note(book):
    """Something to read at the top of the book, after the title and author.

    Extraction drops apparatus — a dedication, a notice, a page of praise — and now and then one
    of those is worth hearing: The Institute opens with 28 words about missing children that no
    length rule was ever going to keep. This is where such a section goes. Capped, because it
    rides the announcement rather than being a chapter of its own: a dedication or a notice, not
    a chapter's worth of prose."""
    return (book.get("opening") or "").strip()[:OPENING_CHARS]

def chapter_intro(book, index):
    """[(phrase, pause_after)] to speak before the chapter — the book's title and author at
    the very top, then the opening note if it has one, the part's name when this chapter opens a
    new part, then the chapter's number, or its title where a book names its chapters rather
    than numbering them. Empty when announcements are off, and for a section with neither: one
    named after its own first words, which the prose is about to read out anyway."""
    if not book.get("announce", True):
        return []
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        return []
    name  = chapters[index].get("name") or ""
    part  = part_of(name)
    label = heading_of(name)
    pieces = []
    # A heading or a name is spoken alone here, with real silence after it, so a full stop inside
    # one is never ending a sentence — and an engine that meets one pauses anyway: "by George
    # R.R. Martin" came out with two pauses in the middle of the name. The opening note goes in
    # as written, being the one thing in the announcement that is prose and has sentences.
    say = lambda phrase, pause: pieces.append((spoken_initials(phrase), pause))

    def say_heading(phrase, pause):
        """A part or a chapter heading, except where the book's title has just said it.

        A page carrying nothing but the title becomes a chapter, or the part above the first
        one: The Time Machine opened "The Time Machine" … "by H G Wells" … "The Time Machine" …
        "1. Introduction". The title is worth saying once.

        The start of it counts too, since such a page is often cut short of the subtitle —
        Frankenstein's says "Frankenstein;" and Max Havelaar's the first half of its title. Four
        characters at least, so a heading isn't dropped for sharing a word with the title."""
        flat = epub.norm(phrase)
        titles = [epub.norm(spoken_title(book)), epub.norm(book.get("title"))]
        if flat and not any(t and t.startswith(flat) and len(flat) >= 4 for t in titles):
            say(phrase, pause)

    if index == first_chapter(book):
        # How a published audiobook opens, and it's what the .m4b plays first as well
        said = spoken_title(book)
        if said:
            say(said, TITLE_PAUSE)
        if book.get("author"):
            by = BY.get((book.get("language") or "")[:2], "by")
            say(f"{by} {book['author']}", AUTHOR_PAUSE)
        # A chunk at a time, so a note of a few sentences is a few ordinary TTS calls rather than
        # one long utterance — the same reason a chapter's segments are chunked. A short beat
        # between them and a proper pause before the book starts.
        note = split_chunks(opening_note(book))
        for ni, chunk in enumerate(note):
            pieces.append((chunk, OPENING_PAUSE if ni == len(note) - 1 else CHUNK_PAUSE))
    # Only when the part actually starts — and a chapter left out doesn't start it, or leaving
    # out the first chapter of a part would take the part's name out of the book with it.
    earlier = [c for c in chapters[:index] if not c.get("skip")]
    if part and not any(part_of(c.get("name")) == part for c in earlier):
        say_heading(spoken_heading(part), PART_PAUSE)
    n = label_number(label)
    if n is not None:
        # As digits, for the engine to say in whatever language it speaks: espeak reads "19" as
        # "nineteen" for an English voice and "negentien" for a Dutch one. Spelling it out here
        # would mean spelling it out in one language — a Dutch book was announcing "oo-nuh".
        pieces.append((str(n), CHAPTER_PAUSE))
    elif title_label(label):
        # A book that names its chapters instead of numbering them — Eragon has "Palancar
        # Valley", never a "chapter two". The title is announced for the same reason a number
        # is, and gets the same real silence after it: read as the first line of the prose it
        # runs straight into the text, and no full stop is long enough to fix that.
        #
        # A heading that carries a number as well is read out whole, so the numeral in it wants
        # putting into digits here: Alice's "CHAPTER I. Down the Rabbit-Hole" would be announced
        # as "chapter eye".
        say_heading(spoken_heading(title_label(label)), CHAPTER_PAUSE)
    return pieces

def part_of(name):
    return (name or "").split(PART_SEP)[0] if PART_SEP in (name or "") else ""

def label_of(name):
    """A chapter's own name without the part it's in: "Chapter 1" out of "The Night Knocker ·
    Chapter 1". What a section put back in is recognised by, so it keeps every piece below the
    part — an inserted section's own name can carry a separator of its own."""
    return (name or "").split(PART_SEP, 1)[1] if PART_SEP in (name or "") else (name or "")

def heading_of(name):
    """A chapter's own heading: the last piece of its name, whatever came before it.

    A table of contents that nests deeper than one part repeats the parent inside the child —
    The Institute has "Escape · Escape · Chapter 2" — and read whole that announces a separator
    out loud and hides the number behind it."""
    return label_of(name).split(PART_SEP)[-1].strip()

def chapters_in(book, part=None):
    """Chapters to narrate — of one part of the book, or of all of it when part is None.

    Chapters marked as left out are not among them, anywhere: not in a whole-book run, not in
    the queue it reports, not in an export, and not in the counts any of those show. The
    heuristics in epub.py catch most apparatus, but a section titled like a chapter and long
    enough to be one — a publisher's list of their other titles, say — reads as prose to them,
    and no pattern could tell it apart without silencing real chapters in some other book.
    """
    chapters = [c for c in book.get("chapters") or [] if not c.get("skip")]
    if not part:
        return chapters
    return [c for c in chapters if part_of(c.get("name")) == part]

def run_parts(ra):
    """The parts a bulk run covers, `[]` meaning the whole book.

    A run used to cover exactly one part or all of them, held in `part`. It can now be added to
    while it runs — asking for a second part while the first is being narrated queues it rather
    than being refused — so the slot carries a list. A slot written in the old shape, on a book
    that was mid-run when this landed, is read through here rather than migrated."""
    parts = ra.get("parts")
    if parts is None:
        parts = [ra["part"]] if ra.get("part") else []
    return [p for p in parts if p]

def run_scope(book):
    """The chapters a bulk run has to work through, in the book's order.

    Derived from the book every time it's wanted, never remembered: what a run covers can grow
    while it runs, and a chapter can be left out from under it."""
    parts = run_parts(book.get("render_all") or {})
    if not parts:
        return chapters_in(book)
    keep = set(parts)
    return [c for c in chapters_in(book) if part_of(c.get("name")) in keep]

def first_chapter(book):
    """Which chapter opens the book: the first one that isn't left out, or None if there's
    nothing to narrate. The title and author are spoken at the top of it, so leaving the
    publisher's front matter out moves the announcement onto whatever now comes first."""
    return next((c["i"] for c in chapters_in(book)), None)

def book_parts(book):
    """The book's top-level divisions, in order, with how much of each is narrated. Stand-alone
    sections that aren't inside a part (an epigraph, say) are reported under ''."""
    out, seen = [], {}
    for c in chapters_in(book):
        p = part_of(c.get("name"))
        if p not in seen:
            seen[p] = {"part": p, "chapters": 0, "ready": 0, "words": 0, "first": c["i"]}
            out.append(seen[p])
        seen[p]["chapters"] += 1
        seen[p]["ready"] += int(c.get("state") == "ready")
        seen[p]["words"] += c.get("words", 0)
    return out

def render_all_worker(book_id):
    """Narrate every chapter the run covers, in order, until done or told to stop.

    Deliberately queues one chapter at a time and waits for it, rather than putting the whole book
    on the queue: an 8-hour job nothing could get in front of would be intolerable, and this way a
    chapter tapped while the run is going is next in line rather than 190th.

    Takes no scope of its own — run_scope reads it from the book's slot each time round, so a
    part added to the run while it's going is picked up by the worker already in flight. One
    worker per book, however much it's asked to do.
    """
    # Told apart from being stopped, and from the book going away: only a run that worked
    # through everything it covers has produced the audiobook the book may have asked for.
    finished = False
    # Chapters taken off the queue by hand while this run was waiting on them. Stepped over rather
    # than asked for again: a run that re-queued them would put each one straight back and make ✕
    # do nothing at all. ⊘ is how you leave one out of the run for good.
    passed_over = set()
    while True:
        book = find_book(book_id)
        if not book or not (book.get("render_all") or {}).get("running"):
            break
        # only "pending" — a chapter that errored is skipped rather than retried forever
        nxt = next((c["i"] for c in run_scope(book)
                    if c.get("state") == "pending" and c["i"] not in passed_over), None)
        if nxt is None:
            finished = True
            break
        token = queue_render(book_id, nxt)
        token["done"].wait()
        if token["dropped"]:
            passed_over.add(nxt)
        # Counted over what the run covers, not the whole book. Reporting 3 of 192 for a run
        # that only ever intended four chapters made a part run look like a whole-book one.
        book = find_book(book_id) or {}
        scope = run_scope(book)
        done = sum(1 for c in scope if c.get("state") == "ready")
        update_book(book_id, lambda b, n=done, t=len(scope):
                    b.setdefault("render_all", {}).update(done=n, total=t))
    update_book(book_id, lambda b: b.setdefault("render_all", {}).update(running=False))
    if finished:
        auto_export(book_id)

def auto_export(book_id):
    """Build the .m4b a finished run was for, if the book asked for one.

    The whole point of narrating overnight is to have an audiobook in the morning, and the run
    finishing and the file existing were two taps apart — taps nobody was there to make. Off by
    default: an export is a few hundred megabytes and hours of ffmpeg on a long book.

    Scoped the way the run was, where that's unambiguous: a run over a single part exports that
    part, and a wider one — several parts, or the whole book — exports the whole book, which is
    the file you'd have asked for by hand anyway.

    Runs in the worker's own thread, which has nothing left to do, so the encode is serialized
    behind the narration that produced it rather than racing it. Its job is in the table like any
    other export's, but nothing is polling it at four in the morning, so the outcome goes to the
    log as well — a nightly run that quietly failed to export should be findable afterwards.
    """
    book = find_book(book_id)
    if not book or not book.get("auto_export"):
        return
    parts = run_parts(book.get("render_all") or {})
    part = parts[0] if len(parts) == 1 else None
    jid = new_job("export")
    log.info("auto-export: building %s%s", book.get("title") or book_id,
             f" · {part}" if part else "")
    export_worker(jid, book_id, part)
    job = jobs.get(jid) or {}
    log.info("auto-export: %s", job.get("error") or f"built {job.get('file')}")

def export_worker(jid, book_id, part=None):
    """Build one .m4b: every narrated chapter, chapter markers, cover art, metadata.

    An audiobook file plays offline in software designed for it — chapters, sleep timer,
    position — which is more than this app's <audio> element will ever do."""
    job = jobs[jid]
    job["status"] = "collecting"
    tmpdir = tempfile.mkdtemp(prefix="m4b-")
    building = None          # the half-written .m4b, once there is one for the finally to clear
    try:
        book = find_book(book_id)
        if not book:
            raise RuntimeError("unknown book")
        audio_dir = book_dir(book_id, "audio")
        wanted = chapters_in(book, part)
        if not wanted:
            raise RuntimeError(f"no chapters in “{part}”")
        parts, marks, clock, skipped, partial = [], [], 0.0, 0, 0
        for c in wanted:
            segs = c.get("segments") or []
            # Whatever has actually been narrated, not only the chapters that finished. A
            # chapter interrupted half-way still has real audio on disk, and leaving it out
            # made an export of a book-in-progress fail with "nothing narrated yet".
            if not segs:
                skipped += 1
                continue
            partial += int(c.get("state") != "ready")
            start = clock
            for s in segs:
                p = os.path.join(audio_dir, s["file"])
                if not os.path.exists(p):
                    continue
                parts.append(p)
                clock += s.get("seconds") or audio_seconds(p)
            if clock > start:
                # inside a part the "Part · Chapter 3" prefix is just noise on every marker
                label = c.get("name") or f"Chapter {c['i']+1}"
                marks.append((start, clock,
                              label.split(PART_SEP, 1)[1] if part and PART_SEP in label else label))
        if not parts:
            raise RuntimeError("nothing narrated yet — render some chapters first")

        listing = os.path.join(tmpdir, "list.txt")
        with open(listing, "w") as f:
            for p in parts:
                f.write("file '%s'\n" % p.replace("'", r"'\''"))
        # ffmetadata carries the chapter marks; TIMEBASE 1/1000 means milliseconds
        title = f"{book['title']} — {part}" if part else book["title"]
        meta = [";FFMETADATA1", f"title={title}", f"album={book['title']}",
                f"artist={book.get('author') or ''}", "genre=Audiobook"]
        for start, end, name in marks:
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start*1000)}",
                     f"END={int(end*1000)}", f"title={name}"]
        metafile = os.path.join(tmpdir, "meta.txt")
        with open(metafile, "w") as f:
            f.write("\n".join(meta) + "\n")

        os.makedirs(book_dir(book_id, "export"), exist_ok=True)
        name = safe_name(title, "audiobook") + ".m4b"
        out  = book_dir(book_id, "export", name)
        # Encoded under a name the library doesn't recognise and renamed when it's whole, the
        # way the index is written. ffmpeg writing straight to the .m4b put a file that grew as
        # you watched it into "Already exported", offering Share and Delete on however much of
        # an audiobook existed so far; the rename is atomic, on the same directory, so a reader
        # sees the finished file or no file. It also means a killed export leaves a .part rather
        # than a truncated .m4b that looks finished.
        building = out + ".part"
        cover = cover_path(book_id, "full") if ensure_cover(book_id) else None

        job.update(status="encoding", seconds=round(clock, 1))
        cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listing,
               "-i", metafile]
        if cover:
            cmd += ["-i", cover]
        cmd += ["-map", "0:a", "-map_metadata", "1"]
        if cover:
            # attached_pic is what makes players show it as the book's artwork
            cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        # 48 kbps AAC mono: the source is already 32 kbps opus, so this adds little loss
        # while staying in the format every audiobook player reads.
        # -f ipod names the muxer the .m4b extension used to pick for us, which the .part name
        # can't: ffmpeg infers the container from the extension and refuses an unknown one.
        # Checked byte-for-byte against what the extension selected — same file, named later.
        cmd += ["-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart",
                "-f", "ipod", building]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(building):
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-300:])
        text = (f"{len(marks)} chapters"
                + (f", {partial} unfinished" if partial else "")
                + (f", {skipped} not narrated" if skipped else ""))
        os.replace(building, out)
        write_export_note(book_id, name, text, round(clock, 1))
        # The name keeps its spaces — it's what the player will show — so the URL has to be
        # encoded rather than handed over raw.
        job.update(status="done", url=f"/export/{book_id}/{urllib.parse.quote(name)}", file=name,
                   text=text, seconds=round(clock, 1))
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
    finally:
        if building and os.path.exists(building):
            os.remove(building)                  # a failed encode leaves nothing behind
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_segment(text, voice, out_path, intro=None, tail_pause=0, respellings=None,
                    lang="", runs=None):
    """One segment = many TTS calls concatenated. run_lock is taken per chunk, not for the
    whole segment, so hours of narration don't starve everything else.

    `intro` is [(phrase, pause_after)] spoken first — the part name and chapter number.
    `tail_pause` is silence appended at the very end, for the last segment of a chapter.
    `respellings` is the book's own pronunciation map, on top of the global one.
    `runs` is [(piece, voice)] for a chapter with a cast, covering the same text in order; the
    pieces are chunked one at a time so a voice change lands on the quotation mark rather than
    wherever the chunker would have cut."""
    tmpdir = tempfile.mkdtemp(prefix="book-")
    parts = []
    try:
        for ii, (phrase, pause) in enumerate(intro or []):
            raw = os.path.join(tmpdir, f"intro-{ii}.wav")
            with run_lock:
                tts_say(voice, respell(phrase, respellings, lang), 1.0, raw)
            parts.append(pad_with_silence(raw, pause, os.path.join(tmpdir, f"intro-{ii}-pad.wav")))
        pieces = [(c, v) for piece, v in (runs or [(text, voice)])
                  for c in split_chunks(piece)]
        for ci, (chunk, chunk_voice) in enumerate(pieces):
            wav = os.path.join(tmpdir, f"{ci:04d}.wav")
            with run_lock:
                tts_say(chunk_voice, respell(chunk, respellings, lang), 1.0, wav)
            parts.append(wav)
        if not parts:
            raise RuntimeError("nothing to say in this segment")
        if tail_pause:
            # pad the final clip rather than adding a silent one, for the same format reason
            parts[-1] = pad_with_silence(parts[-1], tail_pause,
                                         os.path.join(tmpdir, "tail-pad.wav"))
        # The audio directory can vanish underneath a render — changing the narrator deletes
        # it — and ffmpeg's failure then reads as a mysterious "No such file or directory".
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        listing = os.path.join(tmpdir, "list.txt")
        with open(listing, "w") as f:
            for p in parts:
                f.write(f"file '{p}'\n")
        # 32 kbps opus: ~290 MB for a 20 hour book, against 3.5 GB as wav
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                            "-i", listing, "-c:a", "libopus", "-b:a", "32k", out_path],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-200:])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/books")
def api_book_add():
    """Take an EPUB, pull the chapters out, and keep the prose on disk ready to narrate."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no file"), 400
    bid = uuid.uuid4().hex[:12]
    os.makedirs(book_dir(bid, "text"), exist_ok=True)
    src = book_dir(bid, "book.epub")
    f.save(src)
    try:
        meta, chapters, skipped = epub.extract(src)
    except Exception as e:
        shutil.rmtree(book_dir(bid), ignore_errors=True)
        return jsonify(ok=False, msg=f"couldn't read that EPUB: {str(e)[:150]}"), 400
    if not chapters:
        shutil.rmtree(book_dir(bid), ignore_errors=True)
        return jsonify(ok=False, msg="no readable chapters in that EPUB"), 400
    for i, c in enumerate(chapters):
        with open(text_file(bid, i), "w") as fh:
            fh.write(c["text"])
    try:
        raw_cover = epub.cover(src)
    except Exception:
        raw_cover = None
    has_cover = bool(raw_cover) and make_covers(bid, raw_cover)
    # A Dutch book should land on a Dutch voice without being told.
    dutch = (meta.get("language") or "").startswith("nl")
    voice = (piper_voice_ids()[0] if dutch and piper_voice_ids() else "af_heart")
    entry = {"id": bid, "cover": has_cover,
             "title": meta["title"], "author": meta["author"],
             "language": meta["language"], "voice": voice, "announce": True,
             "added": int(time.time()), "updated": int(time.time()),
             "position": {"chapter": 0, "segment": 0, "offset": 0},
             "skipped": skipped_index(skipped),
             "chapters": [{"i": i, "name": c["name"], "words": c["words"],
                           "state": "pending", "segments": [], "error": None}
                          for i, c in enumerate(chapters)]}
    with index_lock:
        items = load_books()
        items.insert(0, entry)
        write_books(items)
    # Guarded here rather than inside the lookup, so that with OPENLIBRARY=0 there is no thread
    # and no request — nothing to reason about, which is the point of the setting.
    if openlib.AUTOMATIC:
        threading.Thread(target=fetch_description,
                         args=(bid, meta["title"], meta["author"]), daemon=True).start()
    return jsonify(ok=True, book=book_summary(entry))

def fetch_description(book_id, title, author):
    """What the book is about, from Open Library, kept on the book.

    Off the upload rather than inside it: the network is someone else's machine, and adding a
    book must neither wait on it nor fail with it. A book that turns up nothing keeps no
    description and says nothing about it — the field in ⚙ is there to type one in, or to ask
    again once the title is right.
    """
    try:
        text, work = openlib.describe(title, author)
    except Exception as e:              # a timeout, a 500, a search that matched nobody
        log.info("no description for %r: %s", title, e)
        return
    if text:
        update_book(book_id, lambda b: b.update(description=text, work=work))

@app.post("/api/books/describe")
def api_book_describe():
    """Ask again — for a book added before this existed, one the search put on the wrong work,
    or one whose description has been cleared and wants filling back in.

    Not gated by OPENLIBRARY: that setting is about what happens on its own. Tapping ↻ is the
    request, and a button that did nothing would be worse than not having one."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        text, work = openlib.describe(book.get("title") or "", book.get("author") or "")
    except Exception as e:
        return jsonify(ok=False, msg=f"couldn't reach Open Library: {str(e)[:120]}"), 502
    if not text:
        return jsonify(ok=False, msg="Open Library has nothing for this one"), 404
    update_book(book["id"], lambda b: b.update(description=text, work=work))
    return jsonify(ok=True, book=find_book(book["id"]))

@app.get("/api/books")
def api_books():
    return jsonify(books=[book_summary(b) for b in load_books()])

@app.get("/api/books/<book_id>")
def api_book(book_id):
    b = find_book(book_id)
    if not b:
        return jsonify(error="unknown book"), 404
    # The reader polls this every 4 s while anything is rendering, so the queue rides along
    # rather than needing a second request. It's global: renders are serialized across books,
    # so this book can be waiting on another one's chapter.
    return jsonify(book=b | {"cover_v": cover_version(book_id), "epub": book_epub(b)},
                   parts=book_parts(b), narrating=render_status(),
                   exports=book_exports(book_id))

@app.get("/api/books/<book_id>/skipped/<int:n>")
def api_book_skipped(book_id, n):
    """The words of a section extraction left out, so one can be read at the top of the book.

    Re-read from the stored EPUB rather than kept in the index: books.json is rewritten after
    every chapter of every render, and twenty sections of prose would be paid for on each. About
    a second for a long book, which is why the page shows a spinner.
    """
    book = find_book(book_id)
    if not book:
        return jsonify(error="unknown book"), 404
    listed = (book.get("skipped") or [])
    if not 0 <= n < len(listed):
        return jsonify(error="no such section"), 404
    skipped, err = epub_sections(book)
    if err:
        return jsonify(error=err), 400
    if not same_section(listed, skipped, n):
        return jsonify(error="the book's sections have changed — re-read the EPUB first"), 409
    return jsonify(name=skipped[n]["name"], words=skipped[n]["words"],
                   why=skipped[n]["why"], text=skipped[n]["text"])

def spine_position(chapters, at):
    """Where a section belongs in the list, given that it belongs after `at` chapters of the
    book's own spine.

    Counted in spine chapters rather than positions, because the list may already have sections
    put back into it and those aren't in the EPUB's count. A section landing on the same boundary
    as one already put back goes after it, which is the order they were asked for in.
    """
    seen = 0
    for pos, c in enumerate(chapters):
        if seen >= at and not c.get("inserted"):
            return pos
        if not c.get("inserted"):
            seen += 1
    return len(chapters)

def epub_sections(book):
    """(the sections extraction left out, an error to answer with) — re-read from the EPUB.

    The stored list is an index of names and counts; the prose is only ever read back on demand,
    since books.json is rewritten after every chapter of every render. Positional, so it's only
    the same list if the same extraction produced it: a rescan since, or an improved heuristic,
    could have moved everything along by one, and reading out — or putting back — the wrong
    section would be a strange way to find that out.
    """
    src = book_dir(book["id"], "book.epub")
    if not os.path.exists(src):
        return None, "the original EPUB isn't stored for this book"
    try:
        _meta, _chapters, skipped = epub.extract(src)
    except Exception as e:
        return None, f"couldn't re-read it: {e}"[:200]
    return skipped, None

def same_section(listed, skipped, n):
    """Whether entry n of the stored list is still entry n of a fresh extraction."""
    return (0 <= n < len(listed) and n < len(skipped)
            and skipped[n]["name"] == listed[n].get("name")
            and skipped[n]["words"] == listed[n].get("words"))

@app.post("/api/books/insert")
def api_book_insert():
    """Put a section extraction left out back in, as a chapter of its own, where the book has it.

    *Read this at the start* takes one at the top of the book, which is what a dedication or a
    notice wants and no use for an afterword, a section that belongs mid-book, or one that wants
    its own marker in the `.m4b`. This is the other half: the section becomes a real chapter —
    narrated, exported, playable, left out again with ⊘ — at the position the spine gives it.

    A chapter's position *is* its number to everything else, so three things make that safe:

    * **no file is renamed.** Every chapter keeps the storage number its files are under, so
      inserting at 1 in a book of 192 rewrites 192 positions in books.json and moves none of the
      ~400 opus files — which is also why there is nothing to roll back if this fails half way.
      The text is written before the index mentions the chapter; the worst case is one orphan
      file that nothing points at.
    * **it refuses while this book has anything in the engine or queued.** A render reads which
      chapter it is about after taking the lock, so it would come through the renumbering intact
      and narrate whatever had moved into the position it was handed. `gen` is bumped as well, for
      the thread started microseconds ago that hasn't registered as waiting yet: it will throw
      away whatever it makes.
    * **the reader's position moves up with the chapters it points at**, and the page remaps the
      player it owns. A player on *another* device holds the same position in a variable nothing
      here can reach, so it plays on — its bookmark will be one chapter out until the book is
      opened again, which is the one rough edge of this and cheaper than stopping the audio on
      every device that might be listening.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        n = int(d.get("section"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which section?"), 400
    listed = book.get("skipped") or []
    if not 0 <= n < len(listed):
        return jsonify(ok=False, msg="no such section"), 404
    why = busy_with(book["id"])
    if why:
        return jsonify(ok=False, msg=why), 409
    skipped, err = epub_sections(book)
    if err:
        return jsonify(ok=False, msg=err), 400
    if not same_section(listed, skipped, n):
        return jsonify(ok=False, msg="the book's sections have changed — re-read the EPUB "
                                     "first"), 409
    section = skipped[n]
    if not (section.get("text") or "").strip():
        return jsonify(ok=False, msg="there's no text in that section to read"), 400
    chapters = book.get("chapters") or []
    # A fumbled double tap would otherwise make two chapters of it, and the second is only
    # removable with ⊘. Matched on the name, which is the section's own and comes from the same
    # extraction — nothing is stored on the skipped entry, which is positional and can't carry a
    # mark through a rescan.
    if any(c.get("inserted") and label_of(c.get("name")) == section["name"] for c in chapters):
        return jsonify(ok=False, msg=f"“{section['name']}” is already in the book"), 409
    at = section.get("at")
    pos = len(chapters) if at is None else spine_position(chapters, max(0, int(at)))
    # It joins the part it lands in front of, so a notice in the middle of "The Night Knocker"
    # groups and announces with that part rather than splitting the part in two. Nothing at the
    # end of the book: an afterword is not part of the last part.
    part = part_of(chapters[pos].get("name")) if pos < len(chapters) else ""
    name = f"{part}{PART_SEP}{section['name']}" if part else section["name"]
    key = max([chapter_key(c) for c in chapters], default=-1) + 1
    os.makedirs(book_dir(book["id"], "text"), exist_ok=True)
    with open(text_file(book["id"], key), "w") as f:
        f.write(section["text"])
    entry = {"i": pos, "key": key, "inserted": True, "name": name, "words": section["words"],
             "state": "pending", "segments": [], "seconds": None, "error": None}
    was = first_chapter(book)

    def apply(b):
        cs = b.get("chapters") or []
        cs.insert(min(pos, len(cs)), entry)
        renumber(cs)
        b["gen"] = b.get("gen", 0) + 1
        p = dict(b.get("position") or {})
        if (p.get("chapter") or 0) >= pos:
            p["chapter"] = (p.get("chapter") or 0) + 1
            b["position"] = p

    update_book(book["id"], apply)
    # Putting a section in above everything hands the title and author to it, so whatever used to
    # open the book has an opening to lose — it now sits one along. Same move as leaving a chapter
    # out (see api_book_skip), and only that one part is re-made; the rest stays on disk.
    after = find_book(book["id"]) or {}
    opened = was is not None and pos <= was
    stale = [was + 1] if opened and (after["chapters"][was + 1].get("segments") or []) else []

    def reopen(b):
        for i in stale:
            b["chapters"][i].update(state="pending", error=None)

    if stale:
        update_book(book["id"], reopen)
        for i in stale:
            queue_render(book["id"], i)
    return jsonify(ok=True, book=find_book(book["id"]), at=pos, name=name, reopened=stale)

@app.get("/api/books/<book_id>/find")
def api_book_find(book_id):
    """Where a word appears in this book, and how it's spelled there. For getting a respelling's
    written form right without leaving the phone — see find_in_book."""
    book = find_book(book_id)
    if not book:
        return jsonify(error="unknown book"), 404
    q = (request.args.get("q") or "").strip()[:80]
    return jsonify(q=q, forms=find_in_book(book, q))

@app.get("/api/books/<book_id>/preview/<int:index>")
def api_book_preview(book_id, index):
    """How a chapter will start, spoken now, before hours are committed to it.

    One chunk — the same ~600 characters the engine is handed at a time, about 45 s of audio for
    ~17 s of work — behind whatever the chapter's announcement will be. That's every expensive
    mistake in the cheapest possible form: the wrong narrator, a name the voice mangles, a title
    that reads badly out loud, an opening note with a line in it you didn't want. The alternative
    was finding out eight hours later.

    Made through _render_segment rather than a shortcut, so what you hear is what a render would
    produce, pauses and all. Answered as audio in the one request — the page has to set src and
    play inside the tap or iOS refuses the sound, the same reason /api/say is a plain GET.

    Cached under a name derived from what is actually spoken, so a second tap costs nothing and a
    changed voice, title, note or pronunciation makes a different file rather than replaying a
    stale one. The old ones for that chapter go: they answer a question nobody will ask again.
    """
    book = find_book(book_id)
    if not book:
        return jsonify(error="unknown book"), 404
    segments = chapter_segments(book, index)
    chunks = split_chunks(segments[0]) if segments else []
    if not chunks:
        return jsonify(error="nothing to read in that chapter"), 404
    voice = book.get("voice") or "af_heart"
    respellings = book.get("respell") or {}
    intro = chapter_intro(book, index)
    lang = book.get("language") or ""
    spoken = ([respell(p, respellings, lang) for p, _ in intro]
              + [respell(chunks[0], respellings, lang)])
    digest = hashlib.sha1("\n".join([voice] + spoken).encode()).hexdigest()[:16]
    d = book_dir(book_id, "preview")
    # Under the chapter's storage number, like its parts: keyed on the position, a section put
    # back in front of it would leave this preview looking like the next chapter's.
    key = key_at(book, index)
    name = f"ch{key:03d}-{digest}.opus"
    if not os.path.exists(os.path.join(d, name)):
        os.makedirs(d, exist_ok=True)
        try:
            _render_segment(chunks[0], voice, os.path.join(d, name), intro=intro, lang=lang,
                            respellings=respellings)
        except Exception as e:
            return jsonify(error=str(e)[:200]), 500
        for old in os.listdir(d):
            if old.startswith(f"ch{key:03d}-") and old != name:
                os.remove(os.path.join(d, old))
    r = send_from_directory(d, name, conditional=True)
    # Content-addressed, so unlike a chapter's parts this one can be cached blind.
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r

@app.post("/api/books/update")
def api_book_update():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    # Changing voice or heading handling makes the existing audio wrong — it was made with the
    # old setting, and mixing two narrators inside one book would be worse than re-rendering.
    # Only the chapter you're actually at is re-made now, though; the rest come back when you
    # reach them, so switching narrator costs one chapter's wait, not the whole book's.
    resets = ((d.get("voice") and d["voice"] != book.get("voice"))
              or (d.get("announce") is not None
                  and bool(d["announce"]) != bool(book.get("announce", True))))
    chapters = book.get("chapters") or []
    # Nothing rendered yet means nothing to throw away: just change it.
    #
    # "Has any chapter audio", not "is any chapter finished". A chapter a stopped render or a
    # restart left half made has real parts on disk, and a render reuses every part it finds — so
    # asking only about finished ones let a narrator change through without discarding them, and
    # the next render of that chapter carried on in the new voice from parts made in the old one.
    # One chapter of the book, read by two people, with nothing saying so.
    if resets and not any(c.get("state") == "ready" or c.get("segments") for c in chapters):
        resets = False
        d.pop("confirm", None)
    resume = None
    if resets:
        ready = [c["i"] for c in chapters if c.get("state") == "ready"]
        # "where you'd carry on from": your listening position, or the furthest chapter that
        # had been narrated if you'd rendered ahead of yourself
        resume = max([(book.get("position") or {}).get("chapter", 0)] + ready) if chapters else 0
    if resets and not d.get("confirm"):
        # Counted the same way the question above is asked, or a book whose only audio is a
        # half-made chapter would offer to discard "0 chapter(s)" and then discard it.
        rendered = sum(1 for c in chapters if c.get("state") == "ready" or c.get("segments"))
        name = (book.get("chapters") or [{}])[resume].get("name", f"chapter {resume + 1}") \
               if book.get("chapters") else ""
        return jsonify(ok=False, needs_confirm=True, rendered=rendered, resume=resume,
                       msg=(f"the audio for {rendered} chapter(s) was made with the old voice "
                            f"and gets discarded — only “{name}” is re-made now"), ), 409
    def rename(b):
        """The fields the opening announcement is made of, applied to whichever copy of the book
        asks — the real one, or a throwaway to see what the announcement would become."""
        if d.get("title"): b["title"] = d["title"][:200]
        if "spoken_title" in d: b["spoken_title"] = (d["spoken_title"] or "").strip()[:200]
        if "opening" in d: b["opening"] = (d["opening"] or "").strip()[:OPENING_CHARS]
        if "description" in d:
            b["description"] = (d["description"] or "").strip()[:openlib.DESCRIPTION_CHARS]
        return b
    # The opening announcement lives inside the first segment of whichever chapter opens the
    # book, so renaming it leaves that one file saying the old name. Re-making it costs a few
    # seconds and throws nothing away — the chapter's other segments stay on disk and the
    # render skips them — so unlike a voice change this doesn't need confirming, it happens.
    #
    # Asked as "would the opening sound different?" rather than field by field: that's the
    # comparison render_chapter itself makes against the record on the chapter, so it can't drift
    # from it, and one expression covers the title, the spoken title and the opening note.
    # On "has it any audio to be wrong", not on "is it finished". Requiring ready meant an edit
    # made while the opening was still being re-recorded from the *previous* edit was stored and
    # never spoken: the render then marked the chapter ready with the older wording, and nothing
    # would ever notice. A chapter part-way through has published segments too, and queueing a
    # second render behind the first is what the lock is for.
    opens = first_chapter(book)
    said = lambda b: [respell(p, b.get("respell") or {}, b.get("language") or "")
                      for p, _ in chapter_intro(b, opens)]
    renamed = bool(opens is not None and not resets
                   and (chapters[opens].get("segments") or [])
                   and said(rename(dict(book))) != said(book))

    def apply(b):
        rename(b)
        if d.get("voice") and tts_engine_of(d["voice"]): b["voice"] = d["voice"]
        if d.get("announce") is not None: b["announce"] = bool(d["announce"])
        # Nothing narrated changes with it — it decides what happens after a run ends — so
        # unlike the narrator it doesn't reset a thing.
        if d.get("auto_export") is not None: b["auto_export"] = bool(d["auto_export"])
        if isinstance(d.get("position"), dict): b["position"] = d["position"]
        if resets:
            b["gen"] = b.get("gen", 0) + 1        # invalidates anything mid-render
            b.setdefault("render_all", {})["running"] = False
            for c in b["chapters"]:
                c.update(state="pending", segments=[], error=None)
        # Pending, but with its segments kept: render_chapter compares the intro it's about to
        # speak against the one on record and deletes only the file that has gone stale.
        if renamed:
            b["chapters"][opens].update(state="pending", error=None)
    update_book(book["id"], apply)
    if resets:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
        # Re-make just the one you'd carry on from, so the new narrator is ready to listen to
        # without re-rendering everything you'd already been through.
        queue_render(book["id"], resume)
    if renamed:
        queue_render(book["id"], opens)
    return jsonify(ok=True, book=find_book(book["id"]), resume=resume, renamed=renamed)

def apply_respell_repair(book_id, plan):
    """Delete the audio a map change invalidated, and put its chapters back to pending.

    The deletion is the work list: render_chapter re-makes a segment exactly when its file is
    missing, which is the same path a resumed render takes. So nothing here needs to record what
    to redo — the gap in the directory says it.

    What each chapter keeps matters as much as what it loses. `segments` is re-read from disk
    rather than emptied, so the parts that are still current stay playable and stay in an export;
    `intro` is left alone, because it records what the opening on disk was made with and
    refreshing it would hide the very staleness this is repairing; `total` is republished by the
    render; `position` survives, since the re-made file has the same name and a length that
    differs by a fraction of a second, and clamping it would lose the reader's place in a
    twenty-hour book over one word. `gen` is not bumped: that means "everything for this book is
    invalid" and pairs with deleting the whole audio directory.
    """
    # A plan is keyed by position, which is not the number the files are under once anything has
    # been put back into the book — so the two are looked up together rather than assumed equal.
    keys = {c["i"]: chapter_key(c) for c in (find_book(book_id) or {}).get("chapters") or []}
    for i, segs in plan.items():
        key = keys.get(i, i)
        for si in segs:
            path = audio_file(book_id, key, si)
            if os.path.exists(path):
                os.remove(path)
        kept = segments_on_disk(book_id, key)
        update_book(book_id, lambda b, n=i, k=kept: b["chapters"][n].update(
            state="pending", error=None, segments=k, done=len(k), seconds=None))

@app.post("/api/books/respell")
def api_book_respell():
    """Set a book's own pronunciations, and re-narrate only what that changes.

    Neither engine takes a pronunciation override, so a name it says wrong can only be fixed by
    respelling it — and the audio already on disk says the old way. Which audio, exactly, is
    what stale_segments answers: usually one segment per occurrence, out of a book of hundreds.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    old = clean_respell(book.get("respell") or {})
    new = clean_respell(d.get("respell") if isinstance(d.get("respell"), dict) else {})
    added, edited, removed = respell_diff(old, new)
    if not (added or edited or removed):
        # Nothing to say differently, so nothing to re-make — and no write, which would only
        # move `updated` and make the page think something happened.
        return jsonify(ok=True, unchanged=True, book=book)
    plan = respell_repair_plan(book, old, new)
    parts = sum(len(v) for v in plan.values())
    # One common word — "the" — makes every segment of every chapter stale. That's correct, and
    # it would silently delete a whole narration, so anything past a couple of chapters says what
    # it's about to cost first. Same shape as the voice change and the rescan.
    if len(plan) > RESPELL_CONFIRM_AT and not d.get("confirm"):
        return jsonify(ok=False, needs_confirm=True, chapters=len(plan), parts=parts,
                       msg=(f"{parts} part(s) across {len(plan)} chapters were narrated with the"
                            f" old pronunciation and get re-made")), 409
    apply_respell_repair(book["id"], plan)
    # The stamp is what marks an older export as saying a word the old way, so it only moves when
    # audio actually went — adding a respelling for a word nothing narrated has yet said changes
    # no existing file, and flagging every export over it would be crying wolf.
    # Kept as a float, and compared against the raw mtime: whole seconds would miss an export
    # built and then invalidated inside the same second, the way cover_version uses milliseconds
    # for two covers replaced in one.
    stamp = {"respell_changed": time.time()} if plan else {}
    update_book(book["id"], lambda b: b.update(respell=new, **stamp))
    # Every affected chapter, not only the one you're at: each is usually a single segment, and
    # leaving the rest pending would take a finished book back to half-narrated over one word.
    # A chapter left out of the narration is repaired on disk but not re-made — render_chapter
    # returns early on it — so it can't sit in the queue for ever.
    fresh = find_book(book["id"]) or {}
    skipped = {c["i"] for c in fresh.get("chapters") or [] if c.get("skip")}
    for i in sorted(set(plan) - skipped):
        queue_render(book["id"], i)
    return jsonify(ok=True, book=fresh, chapters=sorted(plan), parts=parts,
                   added=added, edited=edited, removed=removed)

@app.post("/api/books/delete")
def api_book_delete():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    with index_lock:
        write_books([b for b in load_books() if b.get("id") != book["id"]])
    shutil.rmtree(book_dir(book["id"]), ignore_errors=True)
    return jsonify(ok=True)

def drop_chapter_audio(book_id, index):
    """Throw away everything narrated for one chapter, on disk and in the index.

    For narrating it again from nothing: a render reuses every part still on disk, which is what
    makes resuming cheap and what makes "do it again" a no-op unless the files go first."""
    audio = book_dir(book_id, "audio")
    key = key_at(find_book(book_id), index)
    for name in sorted(os.listdir(audio)) if os.path.isdir(audio) else []:
        if name.startswith(f"ch{key:03d}-s") and name.endswith(".opus"):
            os.remove(os.path.join(audio, name))
    update_book(book_id, lambda b: b["chapters"][index].update(
        state="pending", segments=[], done=0, seconds=None, error=None))

@app.post("/api/books/render")
def api_book_render():
    """Ask for a chapter (and optionally the one after it, to stay ahead of the listener).

    `redo` narrates one that is already finished. Nothing else can: a render keeps every part it
    finds on disk, so asking again for a chapter that has them changes nothing — which is right
    for resuming an interrupted one and no use when the audio itself is what's wrong. A voice that
    mispronounced something, an announcement that was fixed after the fact, a part that came out
    truncated: the way out was to clear the whole book's narration.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    chapters = book.get("chapters") or []
    if d.get("redo"):
        if not 0 <= index < len(chapters):
            return jsonify(ok=False, msg="no such chapter"), 404
        # Not while it's being narrated: the render holds the files it's writing, and a delete
        # underneath it would have it publish parts that aren't there.
        if chapters[index].get("state") == "rendering":
            return jsonify(ok=False, msg="that chapter is being narrated now"), 409
        drop_chapter_audio(book["id"], index)
        book = find_book(book["id"]) or book
        chapters = book.get("chapters") or []
    wanted = [index]
    if d.get("ahead"):
        # staying a chapter ahead of the listener means the next chapter they'll actually
        # hear, so anything left out of the narration isn't it
        wanted += [c["i"] for c in chapters[index + 1:] if not c.get("skip")][:1]
    # Read before the threads go: what the page tells the reader is whether their tap starts
    # narrating or joins a queue, and after starting we'd be counting ourselves.
    ahead = render_depth()
    started = []
    for i in wanted:
        if 0 <= i < len(chapters) and not chapters[i].get("skip") \
                and chapters[i].get("state") in ("pending", "error"):
            queue_render(book["id"], i)
            started.append(i)
    return jsonify(ok=True, started=started, ahead=ahead)

@app.post("/api/books/retry")
def api_book_retry():
    """Narrate every chapter that failed, again.

    A bulk run steps past an `error` chapter rather than retrying it, which is right — a chapter
    whose text has gone would otherwise hold the run up all night — but it means failures
    accumulate quietly: the run reports itself finished and the book is three chapters short with
    nothing saying so out loud.

    Whatever a failure kept, it keeps: a chapter that fell over on part five of eight has four
    real parts on disk, and the render resumes from them exactly as it does after a restart.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    # Left out of the narration *and* failed is not a failure to fix: render_chapter returns
    # early on a skipped chapter, so it would sit in the queue saying "queued" for ever.
    failed = [c["i"] for c in book.get("chapters") or []
              if c.get("state") == "error" and not c.get("skip")]
    # Read before the threads go, for the same reason api_book_render reads it: what the page
    # tells you is whether this starts now or joins a queue, and we'd be counting ourselves.
    ahead = render_depth()

    def reopen(b):
        for i in failed:
            b["chapters"][i].update(state="pending", error=None)

    if failed:
        update_book(book["id"], reopen)
        for i in failed:
            queue_render(book["id"], i)
    return jsonify(ok=True, started=failed, ahead=ahead, book=find_book(book["id"]))

# ---- more than one voice ----

# How many of a character's lines are handed back at once. Enough to recognise them by and to see
# whether the attribution is right; a character with four hundred lines is not read in a panel.
SAYS_LINES = 40

def part_of_run(book, index, n, segments=None):
    """Which part of a chapter the nth quoted run falls in, or None.

    Counted over the segments rather than the chapter text, because a part is what you'd press play
    on — and it's the same walk the render does to know which voice reads what."""
    seen = 0
    for si, seg in enumerate(segments if segments is not None else chapter_segments(book, index)):
        seen += len(cast.quote_spans(seg))
        if seen >= n:
            return si
    return None

def book_cast(book):
    """Everyone the book has been through, with how much they say and where they first turn up.

    Built from the attributions on disk rather than from the voice map, which holds only the names
    that were given a voice: somebody read by the narrator is still in the cast, and still the
    answer to "who is this". Ordered by where they first speak, which is the order you meet them.
    """
    voices = book.get("cast") or {}
    found, parts = {}, {}
    for c in book.get("chapters") or []:
        data = load_attribution(book["id"], chapter_key(c))
        for line in (data or {}).get("lines") or []:
            name = line.get("speaker")
            if not name or name in (cast.NOT_SPEECH, cast.UNKNOWN):
                continue
            e = found.setdefault(name, {"name": name, "lines": 0, "chapters": set(),
                                        "first": {"chapter": c["i"], "name": c.get("name") or "",
                                                  "n": line["n"]}})
            e["lines"] += 1
            e["chapters"].add(c["i"])
    # The segments of a chapter are worked out once however many characters first speak in it.
    for e in found.values():
        at = e["first"]
        if at["chapter"] not in parts:
            parts[at["chapter"]] = chapter_segments(book, at["chapter"])
        at["part"] = part_of_run(book, at["chapter"], at["n"], parts[at["chapter"]])
    return [e | {"chapters": len(e["chapters"]), "voice": voices.get(e["name"]) or ""}
            for e in sorted(found.values(),
                            key=lambda e: (e["first"]["chapter"], e["first"]["n"]))]

def what_they_say(book, who, limit=SAYS_LINES):
    """A character's lines, in order, with the chapter and part each is in.

    Stops as soon as it has enough: a chapter's text is only read when the attribution says they
    speak in it, and a book of two hundred chapters isn't walked to fill a panel of forty lines."""
    out, more = [], 0
    for c in book.get("chapters") or []:
        data = load_attribution(book["id"], chapter_key(c))
        mine = [l for l in (data or {}).get("lines") or [] if l.get("speaker") == who]
        if not mine:
            continue
        if len(out) >= limit:
            more += len(mine)
            continue
        try:
            with open(text_file(book["id"], chapter_key(c))) as f:
                text = chapter_text(book, c["i"], f.read())
        except OSError:
            continue
        spans = cast.quote_spans(text)
        segments = chapter_segments(book, c["i"])
        for line in mine:
            if len(out) >= limit:
                more += 1
                continue
            start, end = spans[line["n"] - 1] if line["n"] - 1 < len(spans) else (0, 0)
            out.append({"chapter": c["i"], "chapter_name": c.get("name") or "",
                        "part": part_of_run(book, c["i"], line["n"], segments),
                        "how": line.get("how"), "text": text[start:end]})
    return out, more

def voices_here(speakers, casting):
    """How many of a chapter's speakers have a voice of their own — what the page shows on the row.

    Recomputed wherever the map moves rather than stored once: handing one character back to the
    narrator changes the answer for every chapter they speak in, and a row still claiming seven
    voices when one of them has gone is the kind of stale number nobody thinks to distrust."""
    return sum(1 for s in speakers or [] if (casting or {}).get(s["name"]))

def attribute_chapter(book_id, index, model=cast.CAST_MODEL, status=lambda _s: None):
    """Work out who speaks each line of one chapter, store it, and give the new speakers a voice.

    Shared by 🎭 and by the render, which asks for this itself rather than reading a chapter in one
    voice — see cast_wanted. `status` reports the slow part to whoever is watching.
    """
    book = find_book(book_id)
    if not book:
        raise RuntimeError("unknown book")
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        raise RuntimeError("no such chapter")
    key = key_at(book, index)
    with open(text_file(book_id, key)) as f:
        raw = f.read()
    # The text the render will speak, not the file: the heading comes off the top of it, and
    # a run number has to mean the same thing in both or every voice after it is wrong.
    text = chapter_text(book, index, raw)
    status(f"asking {model}")
    data = cast.attribute(text, model=model)
    write_attribution(book_id, key, data)
    roster = kokoro_voices()
    narrator = book.get("voice") or "af_heart"

    # The map is the book's, not the chapter's: a character who speaks in four chapters has
    # to sound the same in all four, so attributing another chapter adds to it and never
    # re-casts anyone already in it.
    def apply(b):
        b["cast"] = cast.assign_voices(data["speakers"], narrator, roster, b.get("cast"))
        # How many of this chapter's speakers ended up with a voice of their own. On the
        # chapter because that's what the page has: it's the difference between offering to
        # work this chapter out and saying it already knows.
        #
        # Whatever audio this chapter has was made in one voice, so it no longer says what the
        # book now says it should. Same rule as a pronunciation change.
        b["chapters"][index].update(state="pending", error=None, segments=[],
                                    cast=voices_here(data["speakers"], b["cast"]))

    update_book(book_id, apply)
    drop_chapter_audio(book_id, index)
    return data

def cast_wanted(book, index):
    """Whether a chapter should be worked out before it is narrated.

    True for a book that has a cast and a chapter that isn't in it yet. Playing a chapter asks for
    the next one to be narrated — that's what keeps the reader ahead of the listener — and without
    this, reaching chapter two of a book being read by seven voices quietly went back to one. A
    book with no cast is left alone: nobody asked it for voices, and minutes of GPU per chapter is
    not something to start on its own."""
    return bool(book.get("cast")) and not load_attribution(book["id"], key_at(book, index))

def cast_worker(jid, book_id, index, model):
    """Work out who speaks each line of one chapter, and give each of them a voice.

    A job rather than a reply to the request: a chapter is asked about a window at a time and
    comes back in a minute or two, which is longer than anything should hold a connection.
    """
    job = jobs[jid]
    job["status"] = "reading"
    try:
        # Whether it had audio, asked before the attribution throws it away. Working a chapter out
        # invalidates what was narrated in one voice — that's the point — and a chapter that was
        # ready and is now silent is a hole in the middle of a book nobody would think to look for,
        # so it goes back on the queue. The same thing a pronunciation change does with what it
        # invalidates. A chapter that had nothing is left alone: it was never going to be heard yet.
        chapters = (find_book(book_id) or {}).get("chapters") or []
        had_audio = bool(0 <= index < len(chapters)
                         and (chapters[index].get("segments")
                              or chapters[index].get("state") == "ready"))
        data = attribute_chapter(book_id, index, model,
                                 status=lambda s: job.update(status=s))
        book = find_book(book_id) or {}
        narrator = book.get("voice") or "af_heart"
        voices = book.get("cast") or {}
        if had_audio:
            queue_render(book_id, index)
        job.update(status="done",
                   text=f"{len(data['speakers'])} speakers in {data['quotes']} quoted lines",
                   cast=[s | {"voice": voices.get(s["name"]) or narrator}
                         for s in data["speakers"]],
                   quotes=data["quotes"], tagged=data["tagged"], renarrating=had_audio)
    except Exception as e:
        # Logged as well as reported: this takes minutes, and whoever asked for it has usually
        # stopped watching the panel by the time it falls over — a job nobody is polling any more
        # is a failure with nowhere to go, and the reason it fell over is worth having.
        log.exception("cast: %s chapter %s failed", book_id, index)
        job.update(status="error", error=str(e)[:300])

@app.post("/api/books/cast")
def api_book_cast():
    """Attribute one chapter's speech, so it can be narrated by more than one voice."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    if not (0 <= index < len(book.get("chapters") or [])):
        return jsonify(ok=False, msg="no such chapter"), 404
    jid = new_job("cast")
    model = (d.get("model") or cast.CAST_MODEL).strip()
    threading.Thread(target=cast_worker, args=(jid, book["id"], index, model),
                     daemon=True).start()
    return jsonify(ok=True, job_id=jid)

@app.get("/api/books/<book_id>/cast")
def api_book_cast_all(book_id):
    """The book's whole cast: who speaks, how much, where you first meet them, and who reads them.

    Where they first speak is the question the list couldn't answer — a name on its own is no help
    deciding what a character should sound like, and "Chapter Two, part 1" is a thing you can go
    and listen to."""
    book = find_book(book_id)
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    return jsonify(ok=True, narrator=book.get("voice") or "af_heart", cast=book_cast(book))

@app.get("/api/books/<book_id>/cast/says")
def api_book_cast_says(book_id):
    """What one character says, in order — the other half of deciding whether an attribution is
    right, and the only way to see it without listening to an hour of narration."""
    book = find_book(book_id)
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    who = (request.args.get("speaker") or "").strip()
    if not who:
        return jsonify(ok=False, msg="which speaker?"), 400
    try:
        limit = max(1, min(int(request.args.get("limit") or SAYS_LINES), 200))
    except ValueError:
        limit = SAYS_LINES
    said, more = what_they_say(book, who, limit)
    return jsonify(ok=True, speaker=who, said=said, more=more)

@app.get("/api/books/<book_id>/cast/<int:index>")
def api_book_cast_get(book_id, index):
    """What was worked out for one chapter: the cast with their voices, and every line with who
    says it — which is the only way to see an attribution is wrong without listening to an hour
    of it."""
    book = find_book(book_id)
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    data = load_attribution(book_id, key_at(book, index))
    if not data:
        return jsonify(ok=False, msg="not attributed yet"), 404
    narrator = book.get("voice") or "af_heart"
    voices = book.get("cast") or {}
    return jsonify(ok=True, model=data.get("model"), made=data.get("made"),
                   quotes=data.get("quotes"), tagged=data.get("tagged"),
                   narrator=narrator,
                   cast=[s | {"voice": voices.get(s["name"]) or narrator}
                         for s in data.get("speakers") or []],
                   lines=data.get("lines") or [])

@app.post("/api/books/cast/voice")
def api_book_cast_voice():
    """Give one character a different voice, or hand their lines back to the narrator.

    Every chapter already narrated with them in it goes back to pending: their voice is in that
    audio, and the point of changing it is not to hear the old one again.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    who = (d.get("speaker") or "").strip()
    if not who:
        return jsonify(ok=False, msg="which speaker?"), 400
    voice = (d.get("voice") or "").strip()
    if voice and not tts_engine_of(voice):
        return jsonify(ok=False, msg=f"unknown voice: {voice}"), 400
    # Two characters may share a voice when you say so. The pass never hands out a voice twice, and
    # refusing it here as well looked tidy until the reason to share turned up: a chapter names a
    # character part-way through, so his earlier lines come back under a description — "the man" and
    # "Leighton Vance", two entries for one person. Asking the model which labels are one person
    # catches some of those and not all, and pointing both at one voice is how you finish the job.
    shared = sorted(k for k, v in (book.get("cast") or {}).items() if k != who and v == voice)
    # Read once, here: which chapters they speak in decides both what gets re-narrated and what
    # each row now says, and the files are on disk rather than in the index.
    attributed = {c["i"]: load_attribution(book["id"], chapter_key(c)) or {}
                  for c in book.get("chapters") or []}
    speaks = [i for i, data in attributed.items()
              if any(l.get("speaker") == who for l in data.get("lines") or [])]
    chapters = book.get("chapters") or []
    # Only the ones with audio to lose. A chapter not narrated yet simply gets the new voice when
    # its turn comes.
    reset = [i for i in speaks
             if chapters[i].get("segments") or chapters[i].get("state") == "ready"]

    def apply(b):
        casting = dict(b.get("cast") or {})
        if voice:
            casting[who] = voice
        else:
            casting.pop(who, None)      # no voice of their own: the narrator reads their lines
        b["cast"] = casting
        for i, data in attributed.items():
            if data.get("speakers"):
                b["chapters"][i]["cast"] = voices_here(data["speakers"], casting)
        for i in reset:
            b["chapters"][i].update(state="pending", error=None, segments=[])

    update_book(book["id"], apply)
    for i in reset:
        drop_chapter_audio(book["id"], i)
    return jsonify(ok=True, book=find_book(book["id"]), reset=reset, shared=shared)

@app.post("/api/books/skip")
def api_book_skip():
    """Leave a chapter out of the narration, or put it back.

    A mark, not a deletion: the chapter keeps its number, its text and any audio it already
    has, so putting it back costs nothing and the numbering everything else is stored under —
    text files, audio files, the saved position — doesn't move.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    if not 0 <= index < len(book.get("chapters") or []):
        return jsonify(ok=False, msg="no such chapter"), 404
    skip = bool(d.get("skip", True))
    was = first_chapter(book)
    update_book(book["id"], lambda b: b["chapters"][index].update(skip=skip))
    # Leaving the front matter out moves the title and author onto the chapter that now opens
    # the book — which, if it was narrated already, doesn't say them. Both ends of the move are
    # re-made: the new opening has an announcement to gain, the old one has one to lose. Their
    # other segments stay on disk; render_chapter deletes only the part that has gone stale.
    after = find_book(book["id"]) or {}
    now = first_chapter(after)
    chapters = after.get("chapters") or []
    stale = [] if was == now else \
        [i for i in sorted({was, now} - {None})
         if not chapters[i].get("skip") and (chapters[i].get("segments") or [])]

    def reopen(b):
        for i in stale:
            b["chapters"][i].update(state="pending", error=None)

    if stale:
        update_book(book["id"], reopen)
        for i in stale:
            queue_render(book["id"], i)
    return jsonify(ok=True, book=find_book(book["id"]), reopened=stale)

@app.post("/api/books/clear")
def api_book_clear():
    """Throw away the narration, keep the book. Until now the only ways to clear a book's
    audio were changing the narrator or deleting the whole thing."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    def apply(b):
        b["gen"] = b.get("gen", 0) + 1          # stops anything mid-render being kept
        b.setdefault("render_all", {})["running"] = False
        for c in b.get("chapters") or []:
            c.update(state="pending", segments=[], error=None, seconds=None)
        b["position"] = {"chapter": 0, "segment": 0, "offset": 0}
    update_book(book["id"], apply)
    shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
    shutil.rmtree(book_dir(book["id"], "export"), ignore_errors=True)
    return jsonify(ok=True, book=find_book(book["id"]))

@app.post("/api/books/clear_part")
def api_book_clear_part():
    """Throw away one part of one chapter, keeping the rest.

    The granularity that was missing: *Clear narration* is the whole book and ↻ is a whole chapter,
    and neither is any use when one part came out wrong — a sentence mangled, a file that ended
    early — while the nine around it are an hour of work that is perfectly good.

    The parts *after* the one cleared stay on disk. A render reuses every part it finds, so
    finishing the chapter costs the one part rather than everything from here on; the index
    meanwhile stops at the gap, because playback walks the list in order and can't step over one.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    chapters = book.get("chapters") or []
    try:
        index, part = int(d.get("chapter")), int(d.get("part"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which part?"), 400
    if not (0 <= index < len(chapters)):
        return jsonify(ok=False, msg="no such chapter"), 404
    # Same rule as ↻: the render holds the files it is writing, and deleting one underneath it
    # would have it publish a part that isn't there.
    if chapters[index].get("state") == "rendering":
        return jsonify(ok=False, msg="that chapter is being narrated now"), 409
    key = chapter_key(chapters[index])
    path = audio_file(book["id"], key, part)
    if part < 0 or not os.path.exists(path):
        return jsonify(ok=False, msg="that part isn't narrated"), 404
    os.remove(path)
    kept = segments_on_disk(book["id"], key)
    update_book(book["id"], lambda b: b["chapters"][index].update(
        state="pending", segments=kept, done=len(kept), seconds=None, error=None))
    return jsonify(ok=True, book=find_book(book["id"]), kept=len(kept))

@app.post("/api/books/rescan")
def api_book_rescan():
    """Re-read the stored EPUB — for when extraction has improved since the book was added.

    Keeps the narrated audio, but only when the chapters still line up exactly: same count,
    same word counts, in the same order. If anything moved, the existing audio might belong
    to different text, so it refuses rather than quietly mismatching sound and chapter.

    A section put back in by hand isn't in the EPUB, so it can't be compared with it: the spine is
    matched against the chapters that came from the spine, and the ones put back are spliced in
    again where they were. If the spine has changed they go with everything else, which the
    confirmation says out loud — they're one tap each to put back.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    src = book_dir(book["id"], "book.epub")
    if not os.path.exists(src):
        return jsonify(ok=False, msg="the original EPUB isn't stored for this book"), 400
    try:
        meta, chapters, skipped = epub.extract(src)
    except Exception as e:
        return jsonify(ok=False, msg=f"couldn't re-read it: {str(e)[:150]}"), 400
    old = book.get("chapters") or []
    spine = [c for c in old if not c.get("inserted")]
    put_back = [c for c in old if c.get("inserted")]
    same = (len(chapters) == len(spine)
            and all(c["words"] == o.get("words") for c, o in zip(chapters, spine)))
    if not same and not d.get("confirm"):
        return jsonify(ok=False, needs_confirm=True,
                       msg=(f"the chapters changed ({len(spine)} → {len(chapters)}), so the "
                            "narrated audio no longer matches and would be discarded"
                            + (f", and the {len(put_back)} section(s) you put back in go with it"
                               if put_back else ""))), 409
    # Built before anything is written, because where a chapter's text goes is its storage number
    # and that comes from the chapter it replaces, not from its position in the new list.
    fresh = [{"i": i, "name": c["name"], "words": c["words"],
              "key": chapter_key(spine[i]) if same else i,
              "state": spine[i].get("state", "pending") if same else "pending",
              "segments": spine[i].get("segments", []) if same else [],
              "seconds": spine[i].get("seconds") if same else None,
              # the chapters have to line up for the audio to be kept, and they
              # line up for what was left out of the narration too
              "skip": spine[i].get("skip", False) if same else False,
              # Who speaks in it goes with the audio, for the same reason: the attribution is
              # keyed by run number, which only means anything while the text is the one it was
              # made from. A chapter that moved keeps the file and loses the claim — rendering
              # counts the runs before it trusts one anyway, and 🎭 asks again in a couple of
              # minutes.
              "cast": spine[i].get("cast") if same else None,
              "error": spine[i].get("error") if same else None}
             for i, c in enumerate(chapters)]
    for c, entry in zip(chapters, fresh):
        with open(text_file(book["id"], chapter_key(entry)), "w") as fh:
            fh.write(c["text"])
    if same:
        for c in sorted(put_back, key=lambda c: c["i"]):
            fresh.insert(min(c["i"], len(fresh)), dict(c))
    renumber(fresh)

    def apply(b):
        b["title"], b["author"] = meta["title"], meta["author"]
        b["skipped"] = skipped_index(skipped)
        b["chapters"] = fresh
        if not same:
            b["position"] = {"chapter": 0, "segment": 0, "offset": 0}
    update_book(book["id"], apply)
    if not same:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
    return jsonify(ok=True, kept_audio=same, book=find_book(book["id"]),
                   put_back=len(put_back) if same else 0)

@app.post("/api/books/render_all")
def api_book_render_all():
    """Narrate the whole book, or one part of it — hours of work, so it reports progress and can
    be stopped. A run already going is added to rather than refused.

    Asking for a second part while the first was being narrated used to answer "already" and do
    nothing, which read as a dead button: the run covered 22 chapters of The Institute and the
    other 115 had no way in. There is one run slot per book, so instead of stacking a second
    worker on it the part joins the run in flight — the worker re-reads what it covers between
    chapters — and asking for the whole book widens the run to everything. Nothing is
    interrupted: whatever is narrating goes on narrating, and the addition is queued behind it.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    part = d.get("part") or None
    ra = book.get("render_all") or {}
    if ra.get("running"):
        have = run_parts(ra)
        # A run over the whole book already covers every part there is; so does one that names
        # this part already. Either way there's nothing to add.
        if not have or (part is not None and part in have):
            return jsonify(ok=True, already=True)
        want = [] if part is None else have + [part]
        scope = run_scope(book | {"render_all": ra | {"parts": want, "part": None}})
        done = sum(1 for c in scope if c.get("state") == "ready")
        # part=None as well as parts=…, so the slot can't say two different things about its
        # scope — run_parts prefers the list, and a stale name under it would be a trap.
        update_book(book["id"], lambda b, w=want, n=done, t=len(scope):
                    b["render_all"].update(parts=w, part=None, done=n, total=t))
        return jsonify(ok=True, added=part, widened=part is None, parts=want)
    parts = [] if part is None else [part]
    scope = run_scope(book | {"render_all": {"parts": parts}})
    done = sum(1 for c in scope if c.get("state") == "ready")
    update_book(book["id"], lambda b: b.update(render_all={
        "running": True, "done": done, "total": len(scope), "parts": parts, "part": part}))
    threading.Thread(target=render_all_worker, args=(book["id"],), daemon=True).start()
    return jsonify(ok=True, parts=parts)

@app.post("/api/books/render_stop")
def api_book_render_stop():
    d = request.get_json(force=True, silent=True) or {}
    if not find_book(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown book"), 404
    # the worker checks this between chapters; the one in flight finishes rather than
    # leaving a half-made chapter behind
    update_book(d["id"], lambda b: b.setdefault("render_all", {}).update(running=False))
    return jsonify(ok=True)

@app.post("/api/books/render_cancel")
def api_book_render_cancel():
    """Take one chapter off the queue.

    For a chapter *waiting its turn*. The one being narrated now isn't cancellable — stopping it
    part-way through a ten-minute part would leave a file nothing finishes — and a chapter a
    whole-book run hasn't reached yet isn't on the queue at all: it's a line the panel projects
    from the run, and *Stop narrating the book* is what ends that.
    """
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    if not drop_from_queue(book["id"], index):
        # Not on it, or its turn came while the tap was in flight — both mean the same thing to
        # whoever tapped, and the queue that comes back says so.
        return jsonify(ok=False, msg="that chapter isn't waiting to be narrated",
                       narrating=render_status()), 409
    return jsonify(ok=True, narrating=render_status())

@app.post("/api/books/export")
def api_book_export():
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(error="unknown book"), 404
    jid = new_job("export")
    threading.Thread(target=export_worker, args=(jid, book["id"], d.get("part") or None),
                     daemon=True).start()
    return jsonify(job_id=jid)

@app.post("/api/books/export/delete")
def api_book_export_delete():
    """Throw away one built .m4b, keeping the narration it was made from.

    Until now the only button that removed an export was *Clear narration*, which also deletes
    every chapter's audio — a heavy price for reclaiming a file that can be rebuilt from what
    stays. Three books' exports are 199 MB here, so they're worth being able to drop one at a
    time."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    name = d.get("file") or ""
    path = safe_path(book_dir(book["id"], "export"), name)
    # The extension as much as the path: safe_path keeps the caller inside the export directory,
    # and this keeps it to the files this endpoint is about.
    if not path or not name.endswith(".m4b"):
        return jsonify(ok=False, msg="not an export of this book"), 404
    try:
        os.remove(path)
    except OSError as e:
        return jsonify(ok=False, msg=str(e)[:200]), 500
    # …and what it said about itself, which describes a file that has gone
    note = export_note_path(book["id"], name)
    if os.path.exists(note):
        os.remove(note)
    # The list back, so the page redraws from what's on disk rather than from what it assumes
    # the delete did.
    return jsonify(ok=True, exports=book_exports(book["id"]))

@app.get("/export/<book_id>/<path:filename>")
def book_export(book_id, filename):
    """Any file of this book worth taking away: an export, or the EPUB it was made from. The
    EPUB is asked for under the book's own name, so it arrives called that rather than
    book.epub — see book_file."""
    path = book_file(book_id, filename)
    if not path:
        return jsonify(error="not found"), 404
    r = send_from_directory(os.path.dirname(path), os.path.basename(path),
                            as_attachment=True, download_name=filename, conditional=True)
    # 137 MB over the tailnet to a phone that may or may not keep it — worth a log line saying
    # which it was. See log_transfer.
    return log_transfer(r, f"download {filename}")

@app.get("/get/<book_id>/<path:filename>")
def book_export_page(book_id, filename):
    """The same download with a page around it, for the home-screen app.

    iOS opens a target="_blank" link in a browser view that has no address bar — just Done,
    Back, Share, Reload and Open-in-Safari. Handed the .m4b itself it has no page to show, so
    it renders blank and greys out both Share and Open-in-Safari: the file has arrived and
    there is nothing to do with it. Handed a page it shows the page, which leaves those two
    buttons live — and a download started in real Safari behaves like any other, landing in
    Files where BookPlayer can take it.
    """
    path = book_file(book_id, filename)
    if not path:
        return jsonify(error="not found"), 404
    name = html.escape(filename)
    href = html.escape(f"/export/{book_id}/{urllib.parse.quote(filename)}")
    mb = os.path.getsize(path) / 1e6
    epub = filename.endswith(".epub")
    what = "EPUB" if epub else "audiobook"
    opens = ("Books, or whatever you read EPUBs in, opens it from there."
             if epub else "BookPlayer opens it from the share sheet.")
    page = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
 body {{ margin:0; padding:28px 20px; background:#141322; color:#efeefb; font-size:17px;
        line-height:1.5; font-family:-apple-system,system-ui,sans-serif; }}
 h1 {{ font-size:19px; margin:0 0 6px; word-break:break-word; }}
 .sz {{ color:#9a97b8; font-size:14px; margin-bottom:22px; }}
 a.dl {{ display:block; padding:16px; border-radius:12px; background:#2a2640;
         border:1px solid #3a3557; color:#efeefb; text-decoration:none; text-align:center;
         font-size:17px; }}
 p {{ color:#9a97b8; font-size:14px; margin-top:22px; }}
</style>
<h1>{name}</h1>
<div class="sz">{mb:.1f} MB {what}</div>
<a class="dl" href="{href}" download>⬇ Download it</a>
<p>If nothing happens, tap the compass at the bottom right to open this in Safari and
download it there. The file lands in Files, and {opens}</p>
"""
    return Response(page, mimetype="text/html")

@app.post("/api/books/cover")
def api_book_cover():
    """Replace the cover by hand — for books that declare none, or when you'd rather use a
    different image than the publisher's."""
    bid = request.form.get("id") or ""
    book = find_book(bid)
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no image"), 400
    if not make_covers(bid, f.read()):
        return jsonify(ok=False, msg="couldn't read that image"), 400
    update_book(bid, lambda b: b.update(cover=True))
    return jsonify(ok=True, cover_v=cover_version(bid))

@app.get("/cover/<book_id>/<size>.jpg")
def book_cover(book_id, size):
    if size not in COVER_SIZES:
        return jsonify(error="unknown size"), 404
    if not ensure_cover(book_id) or not os.path.exists(cover_path(book_id, size)):
        return jsonify(error="no cover"), 404
    r = send_from_directory(book_dir(book_id), f"cover-{size}.jpg", conditional=True)
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r

@app.get("/book/<book_id>/<path:filename>")
def book_audio(book_id, filename):
    path = safe_path(book_dir(book_id, "audio"), filename)
    if not path:
        return jsonify(error="not found"), 404
    # Cacheable on purpose: during the lock-screen test no-store made Safari re-fetch the
    # same file several times. send_from_directory handles range requests, so seeking works.
    #
    # Cached but revalidated, not cached blind. A re-rendered part keeps its filename, so a
    # day-long max-age served the copy the browser already had — re-narrating a chapter to
    # change how it opens played back exactly as before, and the audio on disk was right the
    # whole time. must-revalidate costs one conditional request per part and answers 304 when
    # nothing changed, which is what no-store failed to do.
    r = send_from_directory(book_dir(book_id, "audio"), filename, conditional=True)
    r.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return r


def clear_stale_state():
    """The workers live in this process, so a restart kills them. Anything still marked as
    running is a leftover, and would otherwise show a progress bar that never moves.

    The segments a killed render had already finished are kept, though — they're real audio
    on disk, they're playable, and re-rendering the chapter reuses them. Throwing the list
    away made an interrupted chapter look untouched: nothing to play, and an export that
    said "nothing narrated yet" with the files sitting right there."""
    items = load_books()
    changed = False
    for b in items:
        if (b.get("render_all") or {}).get("running"):
            b["render_all"]["running"] = False
            changed = True
        for c in b.get("chapters") or []:
            if c.get("state") == "rendering":
                kept = segments_on_disk(b["id"], chapter_key(c))
                c.update(state="pending", segments=kept, error=None, done=len(kept))
                changed = True
        # An export killed mid-encode leaves its .m4b.part behind. It's never listed, so it
        # would sit there invisibly, and a book exported nightly would keep one per attempt.
        d = book_dir(b["id"], "export")
        for name in os.listdir(d) if os.path.isdir(d) else []:
            if name.endswith(".m4b.part"):
                os.remove(os.path.join(d, name))
    if changed:
        write_books(items)


def segments_on_disk(book_id, key):
    """What a chapter actually has, read from the audio directory rather than from the index.

    Takes the number the files are under rather than the chapter's position, since the caller
    always has the chapter in hand and the two part company once a section has been put back.
    A render empties the segment list before it starts rebuilding it, so a process killed
    part-way leaves finished files that the index no longer mentions.

    Stops at the first gap, because playback walks the list in order, and drops a final file
    ffprobe can't read a duration out of — that one was being written when the process died."""
    out = []
    for si in range(1000):
        path = audio_file(book_id, key, si)
        if not os.path.exists(path):
            break
        seconds = audio_seconds(path)
        if not seconds:
            os.remove(path)
            break
        out.append({"file": os.path.basename(path), "seconds": seconds})
    return out
