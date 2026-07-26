"""Naming voices, including the speakers inside a multi-speaker model.

The worker runs under Piper's own interpreter, but everything here is file naming and JSON —
`from piper import …` happens inside the functions that synthesize, so this imports fine in the
app's venv and never loads a model.
"""
import json

import pytest

import piper_worker


@pytest.fixture(autouse=True)
def voices_dir(tmp_path, monkeypatch):
    """A voices directory of the app's own, so a test never reads the real models."""
    monkeypatch.setattr(piper_worker, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(piper_worker, "_loaded", {})
    return tmp_path


def make_voice(d, vid, speakers=None):
    """An .onnx and its config, the way piper-voices ships them. The .onnx is a stub: nothing
    here loads it."""
    (d / f"{vid}.onnx").write_bytes(b"\0")
    cfg = {"audio": {"sample_rate": 22050}, "num_speakers": len(speakers or [0])}
    if speakers:
        cfg["speaker_id_map"] = {label: i for i, label in enumerate(speakers)}
    (d / f"{vid}.onnx.json").write_text(json.dumps(cfg))


class TestListVoices:
    def test_a_single_speaker_voice_is_one_entry(self, voices_dir):
        make_voice(voices_dir, "nl_NL-ronnie-medium")
        assert piper_worker.list_voices() == [
            {"id": "nl_NL-ronnie-medium", "lang": "nl_NL", "name": "ronnie",
             "quality": "medium"}]

    def test_a_multi_speaker_model_is_listed_once_per_speaker(self, voices_dir):
        """One 76 MB file holds 52 readers, and Dutch has no high-quality Piper voice — so
        picking among speakers is the way to a better one."""
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["2450", "1724", "1666"])
        got = piper_worker.list_voices()
        assert [v["id"] for v in got] == ["nl_NL-mls-medium-2450", "nl_NL-mls-medium-1724",
                                         "nl_NL-mls-medium-1666"]
        assert [v["name"] for v in got] == ["mls 2450", "mls 1724", "mls 1666"]
        assert {v["lang"] for v in got} == {"nl_NL"}

    def test_speakers_come_out_in_the_models_own_order(self, voices_dir):
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["9", "3", "7"])
        assert [v["id"].rsplit("-", 1)[1] for v in piper_worker.list_voices()] == ["9", "3", "7"]

    def test_a_missing_config_is_treated_as_one_speaker(self, voices_dir):
        (voices_dir / "nl_NL-odd-medium.onnx").write_bytes(b"\0")
        assert [v["id"] for v in piper_worker.list_voices()] == ["nl_NL-odd-medium"]


class TestResolve:
    def test_an_ordinary_voice_has_no_speaker(self, voices_dir):
        make_voice(voices_dir, "nl_NL-ronnie-medium")
        path, speaker = piper_worker.resolve("nl_NL-ronnie-medium")
        assert path.name == "nl_NL-ronnie-medium.onnx" and speaker is None

    def test_a_speaker_id_resolves_to_the_shared_model(self, voices_dir):
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["2450", "1724"])
        path, speaker = piper_worker.resolve("nl_NL-mls-medium-1724")
        assert path.name == "nl_NL-mls-medium.onnx"
        assert speaker == 1                      # the position in the model, not the label

    def test_the_bare_model_still_works(self, voices_dir):
        """Whatever the model's own default speaker is."""
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["2450", "1724"])
        assert piper_worker.resolve("nl_NL-mls-medium")[1] is None

    @pytest.mark.parametrize("vid", ["nl_NL-nope-medium", "nl_NL-mls-medium-9999", "", "-"])
    def test_an_unknown_voice_is_an_error(self, voices_dir, vid):
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["2450"])
        with pytest.raises(FileNotFoundError):
            piper_worker.resolve(vid)

    def test_every_listed_voice_resolves(self, voices_dir):
        """The list is what the pickers offer, so anything in it has to be speakable."""
        make_voice(voices_dir, "nl_NL-mls-medium", speakers=["2450", "1724", "1666"])
        make_voice(voices_dir, "nl_BE-rdh-medium")
        for v in piper_worker.list_voices():
            assert piper_worker.resolve(v["id"])
