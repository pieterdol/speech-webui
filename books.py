"""EPUB narration: the book index, rendering chapters to opus, and the .m4b export.

Owns its own storage paths rather than taking them from core, which is what lets the tests
point the whole layer at a tmpdir.
"""
import json, os, re, shutil, subprocess, tempfile, threading, time, urllib.parse, uuid
from contextlib import contextmanager

from flask import jsonify, request, send_from_directory

import epub
from core import app, index_lock, jobs, new_job, run_lock, safe_path, write_json, HERE
from media import audio_seconds, pad_with_silence
from textprep import ONES, TENS, cut_sentences, respell
from tts import piper_voice_ids, tts_engine_of, tts_say

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
CHUNK_CHARS   = 600       # one Kokoro/Piper call ≈45 s of audio ≈17 s of work
render_lock   = threading.Lock()      # one book render at a time
# render_lock serializes renders, but a lock says nothing about who is holding it or who is
# stacked up behind them — so tapping three chapters looked identical to tapping one, and a
# chapter waiting its turn was indistinguishable from a chapter nobody had asked for.
render_state      = {"current": None, "waiting": []}    # each entry: (book_id, chapter index)
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

def book_summary(b):
    """Enough for the library list without shipping every chapter."""
    chapters = b.get("chapters") or []
    ready = sum(1 for c in chapters if c.get("state") == "ready")
    return {k: b.get(k) for k in ("id", "title", "author", "language", "voice",
                                  "added", "position", "cover")} | \
           {"chapters": len(chapters), "ready": ready,
            "cover_v": cover_version(b.get("id")),
            "words": sum(c.get("words", 0) for c in chapters)}

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

@contextmanager
def render_slot(book_id, index):
    """Books this render in as waiting, then — once the caller says the lock is theirs — as
    the one in progress, and clears it however the render ends, including the early returns
    for a chapter that turned out to be ready already.

    Yields the function that marks the switch. Taken as `with render_slot(...), render_lock:`
    so the render body keeps the one level of indentation it had."""
    job = (book_id, index)
    with render_state_lock:
        render_state["waiting"].append(job)
    def started():
        with render_state_lock:
            if job in render_state["waiting"]:
                render_state["waiting"].remove(job)
            render_state["current"] = job
    try:
        yield started
    finally:
        with render_state_lock:
            if job in render_state["waiting"]:
                render_state["waiting"].remove(job)
            if render_state["current"] == job:
                render_state["current"] = None

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
        current, waiting = render_state["current"], list(render_state["waiting"])
    books = {b["id"]: b for b in load_books()}

    def entry(job, state):
        bid, i = job
        b = books.get(bid) or {}
        chapters = b.get("chapters") or []
        c = chapters[i] if 0 <= i < len(chapters) else {}
        return {"book": bid, "title": b.get("title") or "", "chapter": i, "state": state,
                "name": c.get("name") or f"Chapter {i + 1}", "words": c.get("words") or 0,
                "done": len(c.get("segments") or []), "total": c.get("total") or 0}

    # Two threads can be waiting on the same chapter — you tapped it and the bulk run reached
    # it too — and the second one finds it already made and returns. One line in the queue.
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
        for c in chapters_in(b, ra.get("part")):
            if c.get("state") == "pending" and (b["id"], c["i"]) not in seen:
                queue_.append(entry((b["id"], c["i"]), "queued"))
    return {"current": entry(current, "narrating") if current else None, "queue": queue_}

def render_chapter(book_id, index):
    """Render one chapter to opus, a segment at a time. Marks progress in books.json as it
    goes so the page can show it."""
    with render_slot(book_id, index) as started, render_lock:
        started()                       # the lock is ours: waiting becomes narrating
        book = find_book(book_id)
        if not book:
            return
        chapters = book.get("chapters") or []
        if not (0 <= index < len(chapters)):
            return
        chapter = chapters[index]
        if chapter.get("state") == "ready":
            return
        voice = book.get("voice") or "af_heart"
        # Bumped whenever the narrator changes. A chapter that was already being rendered when
        # you switched would otherwise finish in the old voice and be marked ready, leaving one
        # chapter of the book in the wrong voice with nothing to show for it.
        gen = book.get("gen", 0)
        txt_path = book_dir(book_id, "text", f"ch{index:03d}.txt")
        try:
            with open(txt_path) as f:
                text = f.read()
        except OSError as e:
            update_book(book_id, lambda b: b["chapters"][index].update(
                state="error", error=f"missing text: {e}"[:200]))
            return
        # The chapter's own heading line is a bare number ("9") or the title, which the spoken
        # lead-in says better, so it always comes out of the text.
        text = epub.strip_heading(text, chapter.get("name") or "")
        intro = chapter_intro(book, index)

        # The lead-in lives in the chapter's first segment, and a resumed render keeps whatever
        # files are already on disk — so a chapter left half-made before the announcement
        # changed would keep an opening that no longer matches. Only the first one has to go.
        #
        # Recorded respelled, because respelled is what the engine is given: "11/22/63: A Novel"
        # goes in as "11, 22, 63: A Novel", and a change to how a phrase is pronounced leaves the
        # written form identical. Comparing what's written would call that opening current when
        # it no longer is.
        spoken = [respell(p) for p, _ in intro]
        # Split before publishing the state, not after: how many parts a chapter comes to is
        # pure text work, and knowing it up front is the difference between "part 1 of 2" and
        # ten minutes of "starting…" in the queue panel.
        segments = split_segments(text)
        update_book(book_id, lambda b: b["chapters"][index].update(
            state="rendering", error=None, done=0, segments=[], intro=spoken,
            total=len(segments)))
        audio_dir = book_dir(book_id, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        if chapter.get("intro") != spoken:
            stale = os.path.join(audio_dir, f"ch{index:03d}-s00.opus")
            if os.path.exists(stale):
                os.remove(stale)
        made = []
        try:
            for si, seg_text in enumerate(segments):
                # Between segments, not only at the end: deleting a book or changing the
                # narrator used to leave the whole rest of the chapter still to render before
                # anything noticed, which on a long chapter is most of an hour.
                if render_cancelled(book_id, gen):
                    break
                name = f"ch{index:03d}-s{si:02d}.opus"
                out  = os.path.join(audio_dir, name)
                if not os.path.exists(out):
                    # the closing pause belongs to the chapter, so only the last part gets it
                    _render_segment(seg_text, voice, out,
                                    intro=intro if si == 0 else None,
                                    tail_pause=CHAPTER_END_PAUSE if si == len(segments) - 1 else 0)
                made.append({"file": name, "seconds": audio_seconds(out)})
                # publish each finished segment: you can start listening to segment 1 while
                # segment 2 is still being made
                update_book(book_id, lambda b, m=list(made), n=len(segments):
                            b["chapters"][index].update(segments=m, done=len(m), total=n))
            if not render_cancelled(book_id, gen):
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

PART_SEP = " · "     # how epub.py joins a part name to its chapter label

# Spoken lead-in before a chapter's text: "The Night Knocker" … "one" … the prose. The pause
# after each is real silence, not punctuation — a full stop buys about a third of a second,
# which isn't enough to read as "a new chapter is starting".
PART_PAUSE    = 1.2
CHAPTER_PAUSE = 0.9
# And the very top of the book gets its title and author, the way a published audiobook opens.
TITLE_PAUSE   = 0.7
AUTHOR_PAUSE  = 1.6
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
        elif w in ("chapter", "part", "and", "the"):
            pass                                  # the words a heading wraps its number in
        else:
            return None                           # a titled section, not a numbered one
        i += 1
    return total if seen and 0 < total <= 999 else None

def label_number(label):
    """The number of a chapter from its heading, in digits or in words."""
    m = re.search(r"\d+", label or "")
    return int(m.group(0)) if m else word_number(label)

def spoken_title(book):
    """What the opening announcement calls the book. A title is written to be read, not heard:
    "11/22/63: A Novel" has a subtitle no narrator says out loud, and respell can only fix the
    parts of it that follow a rule. So the book carries an optional spoken form, and where it's
    empty the written title is already what you'd say."""
    return (book.get("spoken_title") or "").strip() or (book.get("title") or "")

def chapter_intro(book, index):
    """[(phrase, pause_after)] to speak before the chapter — the book's title and author at
    the very top, the part's name when this chapter opens a new part, then the chapter
    number. Empty when announcements are off, and for a section that is neither numbered nor
    inside a part (an epigraph, say)."""
    if not book.get("announce", True):
        return []
    chapters = book.get("chapters") or []
    if not (0 <= index < len(chapters)):
        return []
    name  = chapters[index].get("name") or ""
    part  = part_of(name)
    label = name.split(PART_SEP, 1)[1] if PART_SEP in name else name
    pieces = []
    if index == 0:
        # How a published audiobook opens, and it's what the .m4b plays first as well
        said = spoken_title(book)
        if said:
            pieces.append((said, TITLE_PAUSE))
        if book.get("author"):
            by = BY.get((book.get("language") or "")[:2], "by")
            pieces.append((f"{by} {book['author']}", AUTHOR_PAUSE))
    if part and not any(part_of(c.get("name")) == part for c in chapters[:index]):
        pieces.append((part, PART_PAUSE))          # only when the part actually starts
    n = label_number(label)
    if n is not None:
        # As digits, for the engine to say in whatever language it speaks: espeak reads "19" as
        # "nineteen" for an English voice and "negentien" for a Dutch one. Spelling it out here
        # would mean spelling it out in one language — a Dutch book was announcing "oo-nuh".
        pieces.append((str(n), CHAPTER_PAUSE))
    return pieces

def part_of(name):
    return (name or "").split(PART_SEP)[0] if PART_SEP in (name or "") else ""

def chapters_in(book, part=None):
    """Chapters belonging to one part of the book, or all of them when part is None."""
    chapters = book.get("chapters") or []
    if not part:
        return chapters
    return [c for c in chapters if part_of(c.get("name")) == part]

def book_parts(book):
    """The book's top-level divisions, in order, with how much of each is narrated. Stand-alone
    sections that aren't inside a part (an epigraph, say) are reported under ''."""
    out, seen = [], {}
    for c in book.get("chapters") or []:
        p = part_of(c.get("name"))
        if p not in seen:
            seen[p] = {"part": p, "chapters": 0, "ready": 0, "words": 0, "first": c["i"]}
            out.append(seen[p])
        seen[p]["chapters"] += 1
        seen[p]["ready"] += int(c.get("state") == "ready")
        seen[p]["words"] += c.get("words", 0)
    return out

def render_all_worker(book_id, part=None):
    """Narrate every chapter, in order, until done or told to stop.

    Deliberately calls render_chapter per chapter rather than holding render_lock for the
    whole book: an 8-hour job that blocked every other render would be intolerable, and this
    way tapping a single chapter gets its turn between two chapters of the bulk run."""
    while True:
        book = find_book(book_id)
        if not book or not (book.get("render_all") or {}).get("running"):
            break
        # only "pending" — a chapter that errored is skipped rather than retried forever
        nxt = next((c["i"] for c in chapters_in(book, part) if c.get("state") == "pending"), None)
        if nxt is None:
            break
        render_chapter(book_id, nxt)
        # Counted over the part being narrated, not the whole book. Reporting 3 of 192 for a
        # run that only ever intended four chapters made a part run look like a whole-book one.
        book = find_book(book_id) or {}
        scope = chapters_in(book, part)
        done = sum(1 for c in scope if c.get("state") == "ready")
        update_book(book_id, lambda b, n=done, t=len(scope):
                    b.setdefault("render_all", {}).update(done=n, total=t))
    update_book(book_id, lambda b: b.setdefault("render_all", {}).update(running=False))

def export_worker(jid, book_id, part=None):
    """Build one .m4b: every narrated chapter, chapter markers, cover art, metadata.

    An audiobook file plays offline in software designed for it — chapters, sleep timer,
    position — which is more than this app's <audio> element will ever do."""
    job = jobs[jid]
    job["status"] = "collecting"
    tmpdir = tempfile.mkdtemp(prefix="m4b-")
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
        safe = re.sub(r"[^\w\- ]+", "", title).strip()[:80] or "audiobook"
        name = f"{safe}.m4b"
        out  = book_dir(book_id, "export", name)
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
        cmd += ["-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-300:])
        # The name keeps its spaces — it's what the player will show — so the URL has to be
        # encoded rather than handed over raw.
        job.update(status="done", url=f"/export/{book_id}/{urllib.parse.quote(name)}", file=name,
                   text=f"{len(marks)} chapters"
                        + (f", {partial} unfinished" if partial else "")
                        + (f", {skipped} not narrated" if skipped else ""),
                   seconds=round(clock, 1))
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_segment(text, voice, out_path, intro=None, tail_pause=0):
    """One segment = many TTS calls concatenated. run_lock is taken per chunk, not for the
    whole segment, so hours of narration don't starve everything else.

    `intro` is [(phrase, pause_after)] spoken first — the part name and chapter number.
    `tail_pause` is silence appended at the very end, for the last segment of a chapter."""
    tmpdir = tempfile.mkdtemp(prefix="book-")
    parts = []
    try:
        for ii, (phrase, pause) in enumerate(intro or []):
            raw = os.path.join(tmpdir, f"intro-{ii}.wav")
            with run_lock:
                tts_say(voice, respell(phrase), 1.0, raw)
            parts.append(pad_with_silence(raw, pause, os.path.join(tmpdir, f"intro-{ii}-pad.wav")))
        for ci, chunk in enumerate(split_chunks(text)):
            wav = os.path.join(tmpdir, f"{ci:04d}.wav")
            with run_lock:
                tts_say(voice, respell(chunk), 1.0, wav)
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
        with open(book_dir(bid, "text", f"ch{i:03d}.txt"), "w") as fh:
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
             "skipped": skipped[:40],
             "chapters": [{"i": i, "name": c["name"], "words": c["words"],
                           "state": "pending", "segments": [], "error": None}
                          for i, c in enumerate(chapters)]}
    with index_lock:
        items = load_books()
        items.insert(0, entry)
        write_books(items)
    return jsonify(ok=True, book=book_summary(entry))

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
    return jsonify(book=b | {"cover_v": cover_version(book_id)},
                   parts=book_parts(b), narrating=render_status())

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
    if resets and not any(c.get("state") == "ready" for c in chapters):
        resets = False
        d.pop("confirm", None)
    resume = None
    if resets:
        ready = [c["i"] for c in chapters if c.get("state") == "ready"]
        # "where you'd carry on from": your listening position, or the furthest chapter that
        # had been narrated if you'd rendered ahead of yourself
        resume = max([(book.get("position") or {}).get("chapter", 0)] + ready) if chapters else 0
    if resets and not d.get("confirm"):
        rendered = sum(1 for c in book.get("chapters") or [] if c.get("state") == "ready")
        name = (book.get("chapters") or [{}])[resume].get("name", f"chapter {resume + 1}") \
               if book.get("chapters") else ""
        return jsonify(ok=False, needs_confirm=True, rendered=rendered, resume=resume,
                       msg=(f"the audio for {rendered} chapter(s) was made with the old voice "
                            f"and gets discarded — only “{name}” is re-made now"), ), 409
    def rename(b):
        """The two fields the opening announcement is made of, applied to whichever copy of the
        book asks — the real one, or a throwaway to see what the announcement would become."""
        if d.get("title"): b["title"] = d["title"][:200]
        if "spoken_title" in d: b["spoken_title"] = (d["spoken_title"] or "").strip()[:200]
        return b
    # The opening announcement lives inside chapter 0's first segment, so renaming the book
    # leaves that one file saying the old name. Re-making it costs a few seconds and throws
    # nothing away — the chapter's other segments stay on disk and the render skips them — so
    # unlike a voice change this doesn't need confirming, it just happens.
    renamed = bool(spoken_title(rename(dict(book))) != spoken_title(book)
                   and not resets and chapters and chapters[0].get("state") == "ready")

    def apply(b):
        rename(b)
        if d.get("voice") and tts_engine_of(d["voice"]): b["voice"] = d["voice"]
        if d.get("announce") is not None: b["announce"] = bool(d["announce"])
        if isinstance(d.get("position"), dict): b["position"] = d["position"]
        if resets:
            b["gen"] = b.get("gen", 0) + 1        # invalidates anything mid-render
            b.setdefault("render_all", {})["running"] = False
            for c in b["chapters"]:
                c.update(state="pending", segments=[], error=None)
        # Pending, but with its segments kept: render_chapter compares the intro it's about to
        # speak against the one on record and deletes only the file that has gone stale.
        if renamed:
            b["chapters"][0].update(state="pending", error=None)
    update_book(book["id"], apply)
    if resets:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
        # Re-make just the one you'd carry on from, so the new narrator is ready to listen to
        # without re-rendering everything you'd already been through.
        threading.Thread(target=render_chapter, args=(book["id"], resume), daemon=True).start()
    if renamed:
        threading.Thread(target=render_chapter, args=(book["id"], 0), daemon=True).start()
    return jsonify(ok=True, book=find_book(book["id"]), resume=resume, renamed=renamed)

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

@app.post("/api/books/render")
def api_book_render():
    """Ask for a chapter (and optionally the one after it, to stay ahead of the listener)."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    try:
        index = int(d.get("chapter"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="which chapter?"), 400
    wanted = [index] + ([index + 1] if d.get("ahead") else [])
    started = []
    for i in wanted:
        chapters = book.get("chapters") or []
        if 0 <= i < len(chapters) and chapters[i].get("state") in ("pending", "error"):
            threading.Thread(target=render_chapter, args=(book["id"], i), daemon=True).start()
            started.append(i)
    return jsonify(ok=True, started=started)

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

@app.post("/api/books/rescan")
def api_book_rescan():
    """Re-read the stored EPUB — for when extraction has improved since the book was added.

    Keeps the narrated audio, but only when the chapters still line up exactly: same count,
    same word counts, in the same order. If anything moved, the existing audio might belong
    to different text, so it refuses rather than quietly mismatching sound and chapter.
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
    same = (len(chapters) == len(old)
            and all(c["words"] == o.get("words") for c, o in zip(chapters, old)))
    if not same and not d.get("confirm"):
        return jsonify(ok=False, needs_confirm=True,
                       msg=(f"the chapters changed ({len(old)} → {len(chapters)}), so the "
                            "narrated audio no longer matches and would be discarded")), 409
    for i, c in enumerate(chapters):
        with open(book_dir(book["id"], "text", f"ch{i:03d}.txt"), "w") as fh:
            fh.write(c["text"])
    def apply(b):
        b["title"], b["author"] = meta["title"], meta["author"]
        b["skipped"] = skipped[:40]
        keep = {o["i"]: o for o in (b.get("chapters") or [])} if same else {}
        b["chapters"] = [{"i": i, "name": c["name"], "words": c["words"],
                          "state": keep.get(i, {}).get("state", "pending"),
                          "segments": keep.get(i, {}).get("segments", []),
                          "seconds": keep.get(i, {}).get("seconds"),
                          "error": keep.get(i, {}).get("error")}
                         for i, c in enumerate(chapters)]
        if not same:
            b["position"] = {"chapter": 0, "segment": 0, "offset": 0}
    update_book(book["id"], apply)
    if not same:
        shutil.rmtree(book_dir(book["id"], "audio"), ignore_errors=True)
    return jsonify(ok=True, kept_audio=same, book=find_book(book["id"]))

@app.post("/api/books/render_all")
def api_book_render_all():
    """Narrate the whole book — hours of work, so it reports progress and can be stopped."""
    d = request.get_json(force=True, silent=True) or {}
    book = find_book(d.get("id") or "")
    if not book:
        return jsonify(ok=False, msg="unknown book"), 404
    if (book.get("render_all") or {}).get("running"):
        return jsonify(ok=True, already=True)
    part = d.get("part") or None
    scope = chapters_in(book, part)
    done = sum(1 for c in scope if c.get("state") == "ready")
    update_book(book["id"], lambda b: b.update(render_all={
        "running": True, "done": done, "total": len(scope), "part": part}))
    threading.Thread(target=render_all_worker, args=(book["id"], part), daemon=True).start()
    return jsonify(ok=True)

@app.post("/api/books/render_stop")
def api_book_render_stop():
    d = request.get_json(force=True, silent=True) or {}
    if not find_book(d.get("id") or ""):
        return jsonify(ok=False, msg="unknown book"), 404
    # the worker checks this between chapters; the one in flight finishes rather than
    # leaving a half-made chapter behind
    update_book(d["id"], lambda b: b.setdefault("render_all", {}).update(running=False))
    return jsonify(ok=True)

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

@app.get("/export/<book_id>/<path:filename>")
def book_export(book_id, filename):
    path = safe_path(book_dir(book_id, "export"), filename)
    if not path:
        return jsonify(error="not found"), 404
    return send_from_directory(book_dir(book_id, "export"), filename,
                               as_attachment=True, conditional=True)

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
                kept = segments_on_disk(b["id"], c["i"])
                c.update(state="pending", segments=kept, error=None, done=len(kept))
                changed = True
    if changed:
        write_books(items)


def segments_on_disk(book_id, index):
    """What a chapter actually has, read from the audio directory rather than from the index.
    A render empties the segment list before it starts rebuilding it, so a process killed
    part-way leaves finished files that the index no longer mentions.

    Stops at the first gap, because playback walks the list in order, and drops a final file
    ffprobe can't read a duration out of — that one was being written when the process died."""
    out = []
    for si in range(1000):
        path = book_dir(book_id, "audio", f"ch{index:03d}-s{si:02d}.opus")
        if not os.path.exists(path):
            break
        seconds = audio_seconds(path)
        if not seconds:
            os.remove(path)
            break
        out.append({"file": os.path.basename(path), "seconds": seconds})
    return out
