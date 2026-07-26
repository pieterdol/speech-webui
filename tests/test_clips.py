"""Clips, their transcripts, and the presets built from them."""
import io
import os

import pytest

import clips
from conftest import needs_ffmpeg


@pytest.fixture
def make_clip():
    """A clip in the index with a file behind it, as an upload leaves things."""
    def _make(clip_id="c1", name="A clip", seconds=3.0, body=b"\0" * 512, **extra):
        path = os.path.join(clips.CLIPS_DIR, f"{clip_id}.wav")
        with open(path, "wb") as f:
            f.write(body)
        item = {"id": clip_id, "name": name, "file": f"{clip_id}.wav",
                "seconds": seconds, "transcript": None, "added": 0}
        item.update(extra)
        clips.write_index(clips.load_index() + [item])
        return item
    return _make


class TestIndex:
    def test_empty_when_there_is_no_file(self):
        assert clips.load_index() == []

    def test_survives_a_corrupt_index(self):
        """An unreadable index shouldn't take the whole page down with it."""
        with open(clips.INDEX_FILE, "w") as f:
            f.write("{ not json")
        assert clips.load_index() == []

    def test_round_trip(self, make_clip):
        make_clip("c1")
        assert [c["id"] for c in clips.load_index()] == ["c1"]

    def test_find_clip(self, make_clip):
        make_clip("c1")
        assert clips.find_clip("c1")["name"] == "A clip"
        assert clips.find_clip("nope") is None

    def test_clip_path_is_inside_the_clips_directory(self, make_clip):
        c = make_clip("c1")
        assert clips.clip_path(c) == os.path.join(clips.CLIPS_DIR, "c1.wav")


class TestPresets:
    def test_empty_and_corrupt(self):
        assert clips.load_presets() == []
        with open(clips.PRESETS_FILE, "w") as f:
            f.write("nonsense")
        assert clips.load_presets() == []

    def test_round_trip_and_find(self):
        clips.write_presets([{"id": "p1", "name": "Me"}])
        assert clips.find_preset("p1")["name"] == "Me"
        assert clips.find_preset("nope") is None


class TestSaveTranscript:
    def test_attaches_to_the_right_clip(self, make_clip):
        make_clip("c1")
        make_clip("c2")
        clips.save_transcript("c2", "hello there")
        assert clips.find_clip("c1")["transcript"] is None
        assert clips.find_clip("c2")["transcript"] == "hello there"

    def test_unknown_clip_changes_nothing(self, make_clip):
        make_clip("c1")
        clips.save_transcript("nope", "hello")
        assert clips.find_clip("c1")["transcript"] is None


class TestClipRoutes:
    def test_listing(self, client, make_clip):
        make_clip("c1")
        make_clip("c2")
        assert {c["id"] for c in client.get("/api/clips").get_json()["clips"]} == {"c1", "c2"}

    def test_delete_removes_the_entry_and_the_file(self, client, make_clip):
        c = make_clip("c1")
        assert os.path.exists(clips.clip_path(c))
        assert client.post("/api/clips/delete", json={"id": "c1"}).get_json()["ok"]
        assert clips.find_clip("c1") is None
        assert not os.path.exists(clips.clip_path(c))

    def test_delete_unknown(self, client):
        assert client.post("/api/clips/delete", json={"id": "nope"}).status_code == 404

    def test_upload_with_no_file(self, client):
        assert client.post("/api/clips", data={}).status_code == 400

    @needs_ffmpeg
    def test_upload_normalizes_and_indexes(self, client, tmp_path):
        """Whatever came off the phone goes in; 24 kHz mono wav comes out."""
        import subprocess
        src = tmp_path / "in.opus"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "1", "-c:a", "libopus", str(src)], check=True, timeout=60)
        r = client.post("/api/clips", data={
            "file": (io.BytesIO(src.read_bytes()), "in.opus"), "name": "From the phone"})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["ok"]
        c = clips.find_clip(body["clip"]["id"])
        assert c["name"] == "From the phone"
        assert os.path.exists(clips.clip_path(c))
        assert c["seconds"] > 0

    def test_serving_a_clip(self, client, make_clip):
        make_clip("c1", body=b"RIFFxxxx")
        assert client.get("/clip/c1.wav").status_code == 200

    def test_cannot_escape_the_clips_directory(self, client):
        assert client.get("/clip/../clips.json").status_code in (301, 400, 404)


class TestPresetRoutes:
    def test_listing(self, client):
        clips.write_presets([{"id": "p1", "name": "Me", "file": "p1.wav"}])
        assert client.get("/api/presets").get_json()["presets"][0]["id"] == "p1"

    def test_save_needs_a_clip(self, client):
        r = client.post("/api/presets", json={"name": "Me", "clip_id": "nope",
                                              "ref_text": "hello"})
        assert r.status_code == 400
        assert "reference clip" in r.get_json()["msg"]

    def test_save_needs_the_reference_transcript(self, client, make_clip):
        """Without it the F5 CLI would download and run its own Whisper, so it's refused
        here rather than discovered several minutes into a render."""
        make_clip("c1")
        r = client.post("/api/presets", json={"name": "Me", "clip_id": "c1", "ref_text": " "})
        assert r.status_code == 400
        assert "what is said" in r.get_json()["msg"]

    def test_delete_unknown(self, client):
        assert client.post("/api/presets/delete", json={"id": "nope"}).status_code == 404

    @needs_ffmpeg
    def test_a_preset_keeps_its_own_copy_of_the_audio(self, client, make_clip, tmp_path):
        """Clips are scratch and presets are meant to last, so deleting the clip a preset was
        built from must not take the preset's reference audio with it."""
        import subprocess
        wav = os.path.join(clips.CLIPS_DIR, "c1.wav")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000",
                        "-t", "2", "-c:a", "pcm_s16le", wav], check=True, timeout=60)
        clips.write_index([{"id": "c1", "name": "src", "file": "c1.wav",
                            "seconds": 2.0, "transcript": "hello", "added": 0}])

        r = client.post("/api/presets", json={"name": "Me", "clip_id": "c1",
                                              "ref_text": "hello"})
        assert r.status_code == 200, r.get_data(as_text=True)
        preset = clips.load_presets()[0]
        own_copy = os.path.join(clips.PRESETS_DIR, preset["file"])
        assert os.path.exists(own_copy)

        client.post("/api/clips/delete", json={"id": "c1"})
        assert os.path.exists(own_copy)         # still there, still usable
