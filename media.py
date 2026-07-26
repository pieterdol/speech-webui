"""ffmpeg and ffprobe. Every call is a subprocess — nothing here decodes audio itself."""
import os, subprocess

def audio_seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=30)
        return round(float(r.stdout.strip()), 1)
    except Exception:
        return 0.0

def normalize_audio(src, dst, seconds=None):
    """Decode anything the browser or phone produces (iOS audio/mp4, Chrome webm/opus,
    m4a/mp3/flac uploads) into 24 kHz mono 16-bit wav — what Whisper and F5-TTS both
    want internally."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", src]
    if seconds: cmd += ["-t", str(seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("ffmpeg couldn't read that audio: " + (r.stderr or "")[-300:])


def pad_with_silence(src, seconds, dst):
    """Append silence to a rendered clip. apad rather than a separately generated silence
    file: it re-encodes this wav in place, so the padding can't disagree with the engine's
    sample rate and break the concat (Kokoro is 24 kHz, Piper voices are 22.05).
    Returns the padded file, or the original if ffmpeg couldn't do it."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", src,
                        "-af", f"apad=pad_dur={seconds}", dst],
                       capture_output=True, text=True, timeout=120)
    return dst if r.returncode == 0 and os.path.exists(dst) else src
