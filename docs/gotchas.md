# Gotchas

Constraints of this machine and of the engines the app drives. All of them are live: each one is here
because working around it is the current behaviour.

## Ollama picks the wrong GPU backend

Ollama ships only a ROCm 7.2 runtime, and ROCm 7 dropped consumer RDNA2 — which is what the RX 6900
XT (gfx1030) is. Left alone Ollama chooses ROCm and the upload to VRAM crawls: `qwen3:8b` hits the
five-minute load timeout with 15 GB free, and the aborted loads keep VRAM allocated until a reboot.
Hiding the GPU from ROCm makes it fall back to the Vulkan backend it already ships, where the same
model loads in **~3 s and runs at 48–81 tok/s** and VRAM behaves — nothing held when idle, ~6.3 GB
resident, back to the ~1.2 GB desktop baseline when `keep_alive` expires:

```bash
HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve
```

This affects everything on this box that uses Ollama, `~/Code/comfy-agent` included.

**Ollama isn't a service.** Started by hand, so after a reboot Chat is down until it runs again, while
the rest of the app is fine. `ollama.service` in this repo fixes that *and* sets the two variables;
install it with the three commands in its header comment.

**Ollama's context is 8192 tokens** here (`num_ctx`). A very long conversation silently loses its
oldest turns — start a new chat rather than growing one forever.

## A download can't finish inside the home-screen app

On the phone the audiobook is *shared*, not downloaded. Two separate iOS behaviours make the download
impossible, and the page detects the app with `navigator.standalone`:

A plain link puts the file on a full-screen "Open in…" splash with the app hidden behind it, and a
home-screen app has no toolbar to leave it with, so the app has to be force-quit (WebKit 236943).
Adding `target="_blank"` escapes that — the link opens in a browser view that does have a Done button
— but that view is handed a file rather than a page, so it renders blank and greys out both its Share
and its Open-in-Safari buttons. Nothing on that screen can save the file.

So **Share the .m4b** fetches the file in the page and hands it to `navigator.share`, and the share
sheet passes it to BookPlayer directly — the Files detour a download would need doesn't happen. It
takes two taps on a large book: iOS opens the sheet only during a fresh tap and a hundred megabytes
over the tailnet outlasts that, so the first tap fetches (showing progress) and the second one shares,
the file kept in hand between them. **Open in Safari** is the fallback, and points at `/get/…` rather
than the file: a page is something that browser view can render, which leaves *its* Open-in-Safari
button live, and a download in real Safari behaves normally and lands in Files.

Every other browser keeps the plain `download` link, which is why this is invisible on the PC.

**Whether a download finished is in `speech.log`.** The access log can't tell you: an abandoned
transfer and a completed one are both a `200` with no size, and iOS discards a file it can't hand
anywhere. So the export route counts the bytes it actually streams — `sent 16384 of 137022062 bytes —
INCOMPLETE · range=- · ua=…` — which separates a phone that never asked from one that read the whole
file and threw it away. The User-Agent is in there because every request arrives from `127.0.0.1`
through the tailnet proxy, so nothing else says which device it was.

## Models and disk

**Whisper model downloads** happen on first use (`small` ≈ 500 MB, `large-v3-turbo` ≈ 1.6 GB) into
`~/.cache/huggingface`; the first transcription after a switch is slower. F5-TTS fetches its own
models on first use too.

**Spoken replies leave wavs in `outputs/`**, one per sentence-chunk rather than one per reply, so a
talkative session accumulates quickly (~190 KB per chunk). They're gitignored and safe to delete
wholesale.

## Voices

**Kokoro has no Dutch voice** — its 54 cover nine languages and Dutch isn't one, which is why Piper is
here. It *can* be forced at Dutch through espeak-ng, but the result is an American accent reading
Dutch spelling. Not exposed in the UI; use a Piper voice.

**Five of Piper's ten Dutch models are installed** — `alex`, `pim` and `ronnie` (nl_NL), `nathalie`
and `rdh` (nl_BE), all *medium*, ~61 MB each in `~/.local/share/piper-tts/voices`. Medium is as good
as Piper's Dutch gets. The rest are `x_low`/`low` variants of speakers already here, plus
`nl_NL-mls-medium`, which is left out on purpose: its 52 readers come from LibriVox recordings and
each one has too little training data to say a short sentence — under about ten words they produce
unrelated audio, landing as a stray number in the middle of otherwise fine prose. Fiction is full of
short sentences, so it can't narrate.

Add other voices by dropping the `.onnx` and `.onnx.json` from huggingface.co/rhasspy/piper-voices
into `~/.local/share/piper-tts/voices` — picked up on the next restart, no code change, and a
multi-speaker model is expanded to one voice per speaker.

**F5-TTS needs the reference transcript.** If it's blank the CLI would download and run its own
Whisper, so the app rejects the request instead. It's auto-filled from Whisper when the reference clip
has already been transcribed, and a saved preset carries it.

## Text before it's spoken

**Fixing a mispronunciation means respelling it**, at one of two levels. `RESPELL` in `textprep.py` is
the global map, for words any book gets wrong — `movies` → `movees`, because espeak clips the `-ies`
to "movis". A book also carries its own, `book["respell"]`, edited from ⚙ and applied on top; where
both name a word the book wins. Kokoro's documented `[word](/phonemes/)` override belongs to the
KPipeline package and not to `kokoro_onnx`, which reads the markup itself out loud, so respelling is
the whole toolkit.

A replacement goes through `re.sub` as a *callable*, never as a template. It's typed in by hand, and
as a template `AC\DC` or `\1` would be read as a backreference and raise inside a render thread.

**A slash between numbers isn't a sound.** `11/22/63` read as written comes out *"eleven slash
twenty-two slash sixty-three"*, so the slash becomes the comma that is the beat between the groups —
`11, 22, 63` — and a leading zero comes off, `02` being read *"zero two"*. The digits themselves are
left to the engine, which says *"elf, tweeëntwintig"* for a Dutch voice and *"eleven, twenty-two"* for
an English one, and reads a four-digit group as a year in both; spelling them out here would mean
spelling them out in one language. Which group is the month is never guessed at — `10/7` is October
7th in an American book and July 10th in a Dutch one — and two single digits are left alone, `1/2`
being a fraction far more often than a date. Prose as much as titles.

**Nor is a thousands separator.** Kokoro's tokenizer reads it as a break between two numbers, so
`140,000,000 miles` comes out *"one hundred forty, zero zero zero, zero zero zero miles"*. Dropping
the commas is the whole fix — the same tokenizer reads `140000000` as *"one hundred forty million"* —
and it needs no spelling out, so a Dutch voice goes on saying the number in Dutch. Groups of exactly
three digits only, which is what keeps it safe there: a comma is the decimal point in Dutch, and `3,5`
is left to be read as *"drie komma vijf"*. The Dutch thousands form, `140.000.000`, is left alone too
— espeak reads that one correctly as it stands.

**A decimal point, on the other hand, is a word.** Kokoro's tokenizer drops it — "3.5 miles"
phonemises as *"three five"* — so it's spoken, which makes this the one rule that has to know what
language it's in. `DECIMAL_WORD` in `textprep.py` holds the word per language: English gets "point",
and Dutch is left out on purpose rather than for want of a word, since there the decimal is a comma
espeak already reads as *"komma"*. A language the map hasn't been told the word for keeps its numbers
as written, which is the safe way round; no language at all means English, which is what the studio
and chat speak. Never after a currency symbol — Kokoro reads "$3.50" as *"three fifty"* unaided,
which is how the amount is said.

The book's language travels the same path the pronunciation map does, as far as `_render_segment`,
and both sides of every comparison the repair scan makes get the same one — otherwise the language
itself would read as a change and re-narrate the book.

**A hyphenated date** gets the same treatment but needs a narrower rule, since between numbers a
hyphen is usually a range: `1914-1918`, `pages 10-20`, `the 2020-21 season`. Only the two forms
carrying a four-digit year are read as a date — `10-02-1986` and `1986-02-10` — and every other hyphen
is left where it is.

**Titles are written out before they're spoken.** `Mr.` reaches the engine as `Mister`, since the full
stop otherwise reads as a sentence break and drops a pause between the title and the name. Same for
`Mrs.`, `Ms.`, `Dr.`, `Prof.`, and for `Jr.`, `Sr.`, `vs.`, `etc.`, `e.g.`, `i.e.`, `approx.`, which
keep the stop only when it also ends the sentence. `St.` is left alone: it's Saint before a name and
Street after one, and nothing here can tell which.

**Initials lose their stops in the announcement**, for the same reason and only there: *"by George
R.R. Martin"* is a letter, a sentence break, a letter, another sentence break, and then the surname.
Announced it's `George R R Martin` — the letters kept and spaced, since `RR` is a word and `R R` is
how the name is said. The opening note is exempt, being the one part of the announcement that is
prose: there a single capital and a stop can be a sentence ending, as in "and so did I. Then he
left". In the prose itself initials keep their stops; only a phrase spoken alone can assume it has no
sentences in it.
