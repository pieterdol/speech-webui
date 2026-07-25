# Local Speech Studio

A phone-friendly web front-end for the local speech tools on this PC. Record (or upload) a
clip, get the text out of it, and turn text back into speech — either with Kokoro's built-in
voices or by cloning a reference clip with F5-TTS. Everything runs locally on CPU; nothing is
sent to a cloud service.

## URLs

| Where | URL |
| --- | --- |
| Phone / tailnet | `https://your-machine.your-tailnet.ts.net:8443` |
| This PC | `http://127.0.0.1:8600` |

**The HTTPS URL matters.** Safari blocks the microphone (`getUserMedia`) and the clipboard
outside a secure context, so over plain http the record button and the Copy button don't work —
only the file picker does. `localhost` counts as secure, which is why the PC URL is fine.

## Running it

```bash
./run.sh        # foreground
./restart.sh    # kill + relaunch in the background (after editing speech.py)
./serve.sh      # one-time: publish on the tailnet over HTTPS via tailscale serve
tail -f speech.log
```

Port 8443 is used because `443` on this tailnet is already taken by another service.

## What it does

**1 · Record or add a clip.** One button records from the mic (Chrome does webm/opus, iOS does
mp4/aac — both are accepted). Everything is normalized through ffmpeg into 24 kHz mono wav in
`clips/`, which is what both Whisper and F5-TTS want internally. The file picker below it covers
the case where the recording was made elsewhere (iOS Voice Memos, exported to Files).

**2 · Speech → text.** faster-whisper on CPU. `small` is the default (a minute of audio in
~10-15 s); `large-v3-turbo` is the accurate, slower option. English and Dutch. The transcript
appears in a copyable block with a **Copy** button, and can be pushed straight into either of
the text fields in panel 3. Transcripts are cached per clip in `clips.json`.

**3 · Text → speech.**

- *Kokoro* — 54 built-in voices, a few seconds per sentence. The language code is derived from
  the voice prefix (`bm_george` → `en-gb`), otherwise British and non-English voices get
  phonemized with US rules and sound wrong.
- *F5-TTS* — clones a voice. Needs the exact transcript of the reference clip, which is
  auto-filled from Whisper when that clip has already been transcribed. Only clone your own
  voice, or one you have permission to use.

**Saved voices.** Once a reference clip sounds right, "Save this voice for reuse" stores it as a
named preset and the app defaults to it from then on — no re-picking the clip or retyping the
transcript. A preset keeps **its own copy** of the audio (trim already applied) in `presets/`, so
it keeps working after the clip it came from is deleted: clips are scratch space, presets last.

**Quality dial.** F5-TTS sampling steps are selectable: standard (32) or faster (16). Measured on
the same sentence, 16 steps took 66 s against ~120 s for 32, and Whisper transcribed the faster
output back word-perfect. Generation time scales with output length, so treat "about a minute a
sentence" as a guide rather than a promise.

Note that a saved voice does **not** make generation faster. F5-TTS is a zero-shot cloner: it
re-derives the voice from the reference on every run, and the minutes go into generating the
output waveform, not learning the voice. Presets save setup effort; the quality dial saves time.

## How it fits together

```
phone ──https──> tailscale serve :8443 ──> 127.0.0.1:8600  speech.py (Flask)
                                              ├── faster-whisper  (in-process, model resident)
                                              ├── kokoro-tts CLI  (~/.local/bin/kokoro-tts)
                                              └── f5-tts CLI      (~/.local/bin/f5-tts)
```

Both TTS engines have their own venvs (`~/.local/share/kokoro-tts`, `~/.local/share/f5-tts`), so
the app shells out to their CLIs rather than duplicating ~2 GB of dependencies. Whisper is the
exception — it's the most-used path, so `faster-whisper` lives in this app's venv and the model
stays resident between requests.

Long jobs are threads writing into a `jobs` dict that the page polls at `/api/status/<id>`, the
same pattern as `~/Code/comfy-webui`. A single lock serializes all model work: F5-TTS saturates
all 12 cores, so overlapping a clone with a transcription would just make both crawl.

## Layout

```
speech.py     Flask app (port 8600). Named speech.py, not app.py, so restart.sh can't
              collide with comfy-webui's (which kills anything matching "app.py").
index.html    the whole UI — one file, no build step
clips/        normalized input clips (gitignored)
presets/      saved voices — own copy of the reference audio (gitignored)
outputs/      generated audio (gitignored)
clips.json    clip index + cached transcripts (gitignored)
presets.json  saved voices: name, reference transcript, source clip (gitignored)
```

## Gotchas

- **No mic on plain http** — use the HTTPS URL. The UI says so if it detects an insecure context.
- **Whisper model downloads** happen on first use (`small` ≈ 500 MB, `large-v3-turbo` ≈ 1.6 GB)
  into `~/.cache/huggingface`; the first transcription after a switch is slower.
- **F5-TTS needs the reference transcript.** If it's blank the CLI would download and run its
  own Whisper, so the app rejects the request instead.
- **Restarting drops running jobs** — the `jobs` dict is in memory. The page says so if you poll
  a job the server no longer knows.
