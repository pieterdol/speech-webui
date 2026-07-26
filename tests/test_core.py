"""The shared plumbing: the atomic index write, the job table, and the path guard.

safe_path is the one security boundary in the app — four routes hand it a filename that came
off the wire — so it gets the most attention here.
"""
import json
import os

import core


class TestWriteJson:
    """A whole-book render rewrites the index after every chapter while the page polls it,
    which is why this is a rename and not a truncate-and-write."""

    def test_round_trips(self, tmp_path):
        p = str(tmp_path / "x.json")
        core.write_json(p, [{"id": "a"}, {"id": "b"}])
        assert json.load(open(p)) == [{"id": "a"}, {"id": "b"}]

    def test_overwrites_in_place(self, tmp_path):
        p = str(tmp_path / "x.json")
        core.write_json(p, [{"id": "a"}])
        core.write_json(p, [{"id": "b"}])
        assert json.load(open(p)) == [{"id": "b"}]

    def test_leaves_no_temp_file(self, tmp_path):
        d = tmp_path / "only-this"
        d.mkdir()
        core.write_json(str(d / "x.json"), [])
        assert sorted(os.listdir(d)) == ["x.json"]

    def test_a_reader_never_sees_half_a_file(self, tmp_path, monkeypatch):
        """The rename is the point: at no moment does the real path hold partial json."""
        p = str(tmp_path / "x.json")
        core.write_json(p, [{"id": "old"}])
        seen = []
        real_replace = os.replace

        def watching_replace(src, dst):
            seen.append(json.load(open(dst)))      # what a reader would get mid-write
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", watching_replace)
        core.write_json(p, [{"id": "new"}])
        assert seen == [[{"id": "old"}]]           # still entirely the previous version


class TestJobs:
    def test_new_job_is_queued_and_unique(self):
        a, b = core.new_job("export"), core.new_job("export")
        assert a != b
        assert core.jobs[a]["status"] == "queued"
        assert core.jobs[a]["kind"] == "export"

    def test_carries_the_fields_the_page_polls(self):
        job = core.jobs[core.new_job("speak")]
        for key in ("error", "text", "url", "file", "seconds", "clip_id",
                    "audio", "audio_done", "audio_error"):
            assert key in job

    def test_status_endpoint(self, client):
        jid = core.new_job("export")
        core.jobs[jid].update(status="done", text="2 chapters")
        got = client.get(f"/api/status/{jid}").get_json()
        assert got["status"] == "done" and got["text"] == "2 chapters"

    def test_unknown_job(self, client):
        """The page says "the server restarted" on this, because that's what it means."""
        assert client.get("/api/status/nope").status_code == 404


class TestSafePath:
    def test_resolves_a_file_inside_the_directory(self, tmp_path):
        f = tmp_path / "a.wav"
        f.write_bytes(b"x")
        assert core.safe_path(str(tmp_path), "a.wav") == os.path.realpath(str(f))

    def test_subdirectories_are_fine(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.wav").write_bytes(b"x")
        assert core.safe_path(str(tmp_path), "sub/a.wav")

    def test_refuses_to_climb_out(self, tmp_path):
        (tmp_path / "inside").mkdir()
        (tmp_path / "secret.json").write_bytes(b"x")
        assert core.safe_path(str(tmp_path / "inside"), "../secret.json") is None

    def test_refuses_an_absolute_path(self, tmp_path):
        assert core.safe_path(str(tmp_path), "/etc/passwd") is None

    def test_refuses_a_deep_climb(self, tmp_path):
        assert core.safe_path(str(tmp_path), "../../../../etc/passwd") is None

    def test_refuses_a_symlink_pointing_out(self, tmp_path):
        """realpath, not string prefixes: a link inside the directory still resolves out."""
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"x")
        inside = tmp_path / "inside"
        inside.mkdir()
        os.symlink(outside, inside / "link.txt")
        assert core.safe_path(str(inside), "link.txt") is None

    def test_refuses_a_directory(self, tmp_path):
        (tmp_path / "sub").mkdir()
        assert core.safe_path(str(tmp_path), "sub") is None

    def test_missing_file(self, tmp_path):
        assert core.safe_path(str(tmp_path), "nope.wav") is None

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path):
        """books-evil must not pass as being under books."""
        (tmp_path / "books").mkdir()
        (tmp_path / "books-evil").mkdir()
        (tmp_path / "books-evil" / "x.txt").write_bytes(b"x")
        assert core.safe_path(str(tmp_path / "books"), "../books-evil/x.txt") is None
