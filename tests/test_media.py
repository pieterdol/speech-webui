"""The ffmpeg helpers, run against real audio.

Small files and short durations, but genuinely encoded and probed — the whole point of these
three functions is what ffmpeg does with them.
"""
import os
import subprocess

import media
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def tone(path, seconds=1.0, rate=24000, codec="libopus"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}",
                    "-t", str(seconds), "-c:a", codec, path], check=True, timeout=60)
    return path


def probe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries,
                          "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, timeout=60).stdout.split()
    return out


class TestAudioSeconds:
    def test_reads_a_duration(self, tmp_path):
        assert abs(media.audio_seconds(tone(str(tmp_path / "a.opus"), 2.0)) - 2.0) < 0.2

    def test_zero_for_a_file_that_is_not_audio(self, tmp_path):
        p = tmp_path / "junk.opus"
        p.write_bytes(b"not audio at all")
        assert media.audio_seconds(str(p)) == 0.0

    def test_zero_for_a_missing_file(self, tmp_path):
        """segments_on_disk relies on this: a falsy duration means the part is unusable."""
        assert media.audio_seconds(str(tmp_path / "nope.opus")) == 0.0

    def test_zero_for_an_empty_file(self, tmp_path):
        p = tmp_path / "empty.opus"
        p.write_bytes(b"")
        assert media.audio_seconds(str(p)) == 0.0


class TestPadWithSilence:
    """Padding the clip itself rather than concatenating a separate silence file — that way
    it can't disagree with the engine's sample rate and break the concat."""

    def test_makes_the_clip_longer(self, tmp_path):
        src = tone(str(tmp_path / "a.wav"), 1.0, codec="pcm_s16le")
        out = media.pad_with_silence(src, 1.5, str(tmp_path / "b.wav"))
        assert out == str(tmp_path / "b.wav")
        assert abs(media.audio_seconds(out) - 2.5) < 0.2

    def test_the_padding_inherits_the_sample_rate(self, tmp_path):
        """Kokoro is 24 kHz and Piper 22.05 — a mismatch here is what breaks concatenation."""
        src = tone(str(tmp_path / "a.wav"), 0.5, rate=22050, codec="pcm_s16le")
        out = media.pad_with_silence(src, 0.5, str(tmp_path / "b.wav"))
        assert probe(out, "stream=sample_rate") == ["22050"]

    def test_falls_back_to_the_original_if_ffmpeg_cannot(self, tmp_path):
        bad = tmp_path / "junk.wav"
        bad.write_bytes(b"not audio")
        out = media.pad_with_silence(str(bad), 1.0, str(tmp_path / "b.wav"))
        assert out == str(bad)          # the caller still gets something playable-ish


class TestNormalizeAudio:
    """Whatever the browser or phone produced, in: 24 kHz mono 16-bit wav out, which is what
    Whisper and F5 both want."""

    def test_converts_to_24k_mono(self, tmp_path):
        src = tone(str(tmp_path / "a.opus"), 1.0, rate=48000)
        out = str(tmp_path / "b.wav")
        media.normalize_audio(src, out)
        assert probe(out, "stream=sample_rate,channels") == ["24000", "1"]

    def test_trims_when_asked(self, tmp_path):
        """A preset's reference clip is cut to 10 s — F5 gains nothing from more."""
        src = tone(str(tmp_path / "a.wav"), 4.0, codec="pcm_s16le")
        out = str(tmp_path / "b.wav")
        media.normalize_audio(src, out, seconds=1.0)
        assert abs(media.audio_seconds(out) - 1.0) < 0.2

    def test_no_trim_keeps_the_whole_thing(self, tmp_path):
        src = tone(str(tmp_path / "a.wav"), 2.0, codec="pcm_s16le")
        out = str(tmp_path / "b.wav")
        media.normalize_audio(src, out)
        assert abs(media.audio_seconds(out) - 2.0) < 0.2
