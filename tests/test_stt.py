"""Transcription.

Whisper itself is never loaded — that's a 500 MB download and several seconds a call, and it
isn't this app's code. What is this app's code is which model gets asked for, what happens to
the text afterwards, and the fact that dictation audio is scratch that must not survive.
"""
import os

import pytest

import clips
import core
import stt


@pytest.fixture
def fake_whisper(monkeypatch):
    """Stand in for run_stt, recording what it was asked to transcribe."""
    calls = []

    def _run(job, path, model_name, lang):
        calls.append({"path": path, "model": model_name, "lang": lang})
        job["status"] = "transcribing"
        return "the transcribed words", 4.2

    monkeypatch.setattr(stt, "run_stt", _run)
    return calls


@pytest.fixture
def a_clip():
    path = os.path.join(clips.CLIPS_DIR, "c1.wav")
    with open(path, "wb") as f:
        f.write(b"\0" * 256)
    clips.write_index([{"id": "c1", "name": "A clip", "file": "c1.wav",
                        "seconds": 3.0, "transcript": None, "added": 0}])
    return path


class TestModelChoice:
    def test_the_whitelist(self):
        assert stt.DEFAULT_STT in stt.STT_MODELS

    def test_an_unknown_model_falls_back(self, client, a_clip, fake_whisper):
        client.post("/api/transcribe", json={"clip_id": "c1", "model": "gpt-9"})
        assert fake_whisper[0]["model"] == stt.DEFAULT_STT

    def test_a_known_model_is_used(self, client, a_clip, fake_whisper):
        client.post("/api/transcribe", json={"clip_id": "c1", "model": "large-v3-turbo"})
        assert fake_whisper[0]["model"] == "large-v3-turbo"

    def test_an_unknown_language_falls_back_to_english(self, client, a_clip, fake_whisper):
        client.post("/api/transcribe", json={"clip_id": "c1", "language": "kl"})
        assert fake_whisper[0]["lang"] == "en"

    def test_dutch_is_accepted(self, client, a_clip, fake_whisper):
        client.post("/api/transcribe", json={"clip_id": "c1", "language": "nl"})
        assert fake_whisper[0]["lang"] == "nl"


class TestTranscribeWorker:
    def test_saves_the_text_onto_the_clip(self, a_clip, fake_whisper):
        jid = core.new_job("transcribe")
        stt.transcribe_worker(jid, clips.find_clip("c1"), "small", "en")
        assert core.jobs[jid]["status"] == "done"
        assert core.jobs[jid]["text"] == "the transcribed words"
        assert clips.find_clip("c1")["transcript"] == "the transcribed words"

    def test_reports_a_failure_rather_than_raising(self, a_clip, monkeypatch):
        def boom(job, path, model_name, lang):
            raise RuntimeError("model exploded")
        monkeypatch.setattr(stt, "run_stt", boom)
        jid = core.new_job("transcribe")
        stt.transcribe_worker(jid, clips.find_clip("c1"), "small", "en")
        assert core.jobs[jid]["status"] == "error"
        assert "model exploded" in core.jobs[jid]["error"]
        assert clips.find_clip("c1")["transcript"] is None


class TestDictateWorker:
    """A spoken chat turn: the words are the point, the audio is scratch."""

    def test_the_audio_is_deleted_afterwards(self, tmp_path, fake_whisper):
        scratch = tmp_path / "spoken.wav"
        scratch.write_bytes(b"\0" * 128)
        jid = core.new_job("dictate")
        stt.dictate_worker(jid, str(scratch), "small", "en")
        assert core.jobs[jid]["text"] == "the transcribed words"
        assert not scratch.exists()

    def test_deleted_even_when_transcription_fails(self, tmp_path, monkeypatch):
        scratch = tmp_path / "spoken.wav"
        scratch.write_bytes(b"\0" * 128)
        monkeypatch.setattr(stt, "run_stt",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("nope")))
        jid = core.new_job("dictate")
        stt.dictate_worker(jid, str(scratch), "small", "en")
        assert core.jobs[jid]["status"] == "error"
        assert not scratch.exists()

    def test_it_never_enters_the_clip_index(self, tmp_path, fake_whisper):
        scratch = tmp_path / "spoken.wav"
        scratch.write_bytes(b"\0" * 128)
        stt.dictate_worker(core.new_job("dictate"), str(scratch), "small", "en")
        assert clips.load_index() == []


class TestTranscribeRoute:
    def test_unknown_clip(self, client):
        assert client.post("/api/transcribe", json={"clip_id": "nope"}).status_code == 404

    def test_clip_whose_file_has_gone(self, client):
        clips.write_index([{"id": "c1", "name": "x", "file": "gone.wav",
                            "seconds": 1, "transcript": None, "added": 0}])
        r = client.post("/api/transcribe", json={"clip_id": "c1"})
        assert r.status_code == 404
        assert "missing" in r.get_json()["error"]

    def test_returns_a_job_to_poll(self, client, a_clip, fake_whisper):
        body = client.post("/api/transcribe", json={"clip_id": "c1"}).get_json()
        assert body["job_id"] in core.jobs


class TestModelCache:
    """One model resident at a time — two int8 Whispers in RAM just contend for the same
    twelve cores on the next call."""

    def test_loads_once_and_reuses(self, monkeypatch):
        loads = []

        class FakeModel:
            def __init__(self, name, **kw):
                loads.append(name)

        monkeypatch.setattr(stt, "_stt", {"name": None, "model": None})
        monkeypatch.setitem(__import__("sys").modules, "faster_whisper",
                            type("m", (), {"WhisperModel": FakeModel}))
        stt.stt_model("small")
        stt.stt_model("small")
        assert loads == ["small"]

    def test_switching_model_reloads(self, monkeypatch):
        loads = []

        class FakeModel:
            def __init__(self, name, **kw):
                loads.append(name)

        monkeypatch.setattr(stt, "_stt", {"name": None, "model": None})
        monkeypatch.setitem(__import__("sys").modules, "faster_whisper",
                            type("m", (), {"WhisperModel": FakeModel}))
        stt.stt_model("small")
        stt.stt_model("large-v3-turbo")
        assert loads == ["small", "large-v3-turbo"]
