"""Speech synthesis.

Neither engine is started — that's a resident subprocess with its own interpreter and a
few hundred MB of model. What's tested is which engine a voice belongs to, how the voice list
is built off the filesystem, and what the endpoints do with a job.
"""
import os

import pytest

import core
import tts


@pytest.fixture
def fake_say(monkeypatch):
    """Stand in for tts_say, writing a plausible file instead of speaking."""
    calls = []

    def _say(voice, text, speed, out_path):
        calls.append({"voice": voice, "text": text, "speed": speed, "out": out_path})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"RIFF" + b"\0" * 512)

    monkeypatch.setattr(tts, "tts_say", _say)
    monkeypatch.setattr(tts, "audio_seconds", lambda p: 1.5)
    return calls


class TestEngineOf:
    """A voice name decides which engine speaks it. Kokoro has no Dutch, which is why Piper
    is here at all, so getting this wrong means the wrong language entirely."""

    def test_a_kokoro_voice(self, monkeypatch):
        monkeypatch.setattr(tts, "kokoro_voices", lambda: ["af_heart", "bm_george"])
        monkeypatch.setattr(tts, "piper_voice_ids", lambda: ["nl_NL-nathalie-medium"])
        assert tts.tts_engine_of("af_heart") == "kokoro"

    def test_a_piper_voice(self, monkeypatch):
        monkeypatch.setattr(tts, "kokoro_voices", lambda: ["af_heart"])
        monkeypatch.setattr(tts, "piper_voice_ids", lambda: ["nl_NL-nathalie-medium"])
        assert tts.tts_engine_of("nl_NL-nathalie-medium") == "piper"

    def test_an_unknown_voice_belongs_to_neither(self, monkeypatch):
        monkeypatch.setattr(tts, "kokoro_voices", lambda: ["af_heart"])
        monkeypatch.setattr(tts, "piper_voice_ids", lambda: [])
        assert tts.tts_engine_of("no_such_voice") is None


class TestVoiceLists:
    """The engine is the authority on which voices it has — the worker scans its own model
    directory — so this is about the caching and the failure path, not the scan."""

    def test_asked_for_once_and_remembered(self, monkeypatch):
        calls = []

        def answer(engine, payload, timeout=None):
            calls.append(engine)
            return {"voices": ["af_heart", "bm_george"]}

        monkeypatch.setattr(tts, "worker_call", answer)
        assert tts.kokoro_voices() == ["af_heart", "bm_george"]
        assert tts.kokoro_voices() == ["af_heart", "bm_george"]
        assert calls == ["kokoro"]           # the second press doesn't start anything

    def test_an_engine_that_will_not_start_means_no_voices(self, monkeypatch):
        """Piper missing shouldn't take the voice picker — or the page — down with it."""
        def boom(engine, payload, timeout=None):
            raise RuntimeError("no such interpreter")

        monkeypatch.setattr(tts, "worker_call", boom)
        assert tts.piper_voices() == []
        assert tts.piper_voice_ids() == []

    def test_ids_are_pulled_out_of_the_records(self, monkeypatch):
        monkeypatch.setattr(tts, "worker_call", lambda *a, **k: {
            "voices": [{"id": "nl_NL-nathalie-medium", "name": "nathalie"},
                       {"id": "nl_BE-rdh-medium", "name": "rdh"}]})
        assert tts.piper_voice_ids() == ["nl_NL-nathalie-medium", "nl_BE-rdh-medium"]


class TestVoiceLanguage:
    """Without this a British voice is phonemized with US rules and sounds wrong."""

    @pytest.mark.parametrize("voice,lang", [
        ("af_heart", "en-us"), ("bm_george", "en-gb"), ("jf_alpha", "ja"),
        ("ff_siwis", "fr-fr"), ("zm_yunjian", "cmn"),
    ])
    def test_prefix_maps_to_a_language(self, voice, lang):
        assert tts.VOICE_LANG[voice[:2]] == lang


class TestVoicesRoute:
    def test_lists_both_engines(self, client, monkeypatch):
        monkeypatch.setattr(tts, "kokoro_voices", lambda: ["af_heart", "bm_george"])
        monkeypatch.setattr(tts, "piper_voices", lambda: [{"id": "nl_NL-nathalie-medium",
                                                           "name": "nathalie"}])
        body = client.get("/api/voices").get_json()
        assert body["voices"] == ["af_heart", "bm_george"]
        assert body["piper"][0]["id"] == "nl_NL-nathalie-medium"
        assert body["lang_of"]["af"] == "en-us"


class TestSpeakRoute:
    def test_empty_text_is_refused(self, client):
        assert client.post("/api/speak", json={"text": "   "}).status_code == 400

    def test_returns_a_job(self, client, fake_say, monkeypatch):
        monkeypatch.setattr(tts, "tts_engine_of", lambda v: "kokoro")
        body = client.post("/api/speak", json={"text": "hello", "voice": "af_heart"}).get_json()
        assert body["job_id"] in core.jobs

    def test_markdown_is_stripped_when_asked(self, client, fake_say, monkeypatch):
        """The chat panel sends its reply verbatim and asks for the markup to come out here,
        so there's one implementation of that rather than one per caller."""
        monkeypatch.setattr(tts, "tts_engine_of", lambda v: "kokoro")
        jid = client.post("/api/speak", json={"text": "**bold** words", "voice": "af_heart",
                                              "strip": True}).get_json()["job_id"]
        for _ in range(50):
            if core.jobs[jid]["status"] in ("done", "error"):
                break
            import time as _t
            _t.sleep(0.05)
        assert fake_say and "**" not in fake_say[0]["text"]


class TestOutputs:
    def test_serving_a_rendered_file(self, client):
        with open(os.path.join(tts.OUT_DIR, "x.wav"), "wb") as f:
            f.write(b"RIFF")
        assert client.get("/out/x.wav").status_code == 200

    def test_deleting_one(self, client):
        p = os.path.join(tts.OUT_DIR, "x.wav")
        with open(p, "wb") as f:
            f.write(b"RIFF")
        assert client.post("/api/out/delete", json={"file": "x.wav"}).get_json()["ok"]
        assert not os.path.exists(p)

    def test_delete_refuses_a_path_outside(self, client):
        r = client.post("/api/out/delete", json={"file": "../clips.json"})
        assert r.status_code == 400
        assert r.get_json()["ok"] is False

    def test_delete_a_file_that_is_not_there(self, client):
        assert client.post("/api/out/delete", json={"file": "nope.wav"}).status_code == 400


class TestSampleRoute:
    def test_unknown_voice(self, client, monkeypatch):
        monkeypatch.setattr(tts, "tts_engine_of", lambda v: None)
        assert client.get("/api/sample/no_such_voice").status_code == 404

    def test_a_cached_sample_is_served_without_rendering(self, client, monkeypatch):
        """Samples are cached one wav per voice; the second press shouldn't touch an engine."""
        monkeypatch.setattr(tts, "tts_engine_of", lambda v: "kokoro")
        called = []
        monkeypatch.setattr(tts, "tts_say", lambda *a: called.append(a))
        with open(os.path.join(tts.SAMPLES_DIR, "af_heart.wav"), "wb") as f:
            f.write(b"RIFF" + b"\0" * 64)
        assert client.get("/api/sample/af_heart").status_code == 200
        assert called == []
