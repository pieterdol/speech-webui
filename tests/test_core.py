"""The shared plumbing: the atomic index write, the job table, and the path guard.

safe_path is the one security boundary in the app — four routes hand it a filename that came
off the wire — so it gets the most attention here.
"""
import json
import os

import flask

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


class TestLogTransfer:
    """What the export route is instrumented with. A 200 in the access log says a download
    started; only this says whether it finished, which is the whole question when a phone
    accepts a file and then throws it away."""

    def wrap(self, chunks, total=None, headers=None, what="export A.m4b"):
        """A file response the way send_from_directory leaves one, wrapped and handed back."""
        with core.app.test_request_context("/export/b1/A.m4b", headers=headers or {}):
            r = flask.Response(iter(chunks), direct_passthrough=True)
            if total is not None:
                r.headers["Content-Length"] = str(total)
            return core.log_transfer(r, what)

    def test_counts_what_went_out(self, caplog):
        with caplog.at_level("INFO", logger="speech"):
            r = self.wrap([b"a" * 10, b"b" * 10], total=20)
            assert b"".join(r.response) == b"a" * 10 + b"b" * 10
        assert "sent 20 of 20 bytes" in "\n".join(caplog.messages)
        assert "INCOMPLETE" not in "\n".join(caplog.messages)

    def test_an_abandoned_transfer_says_so(self, caplog):
        """The phone opening the file, taking a look and closing the view. Closing the iterator
        is what a WSGI server does when the client goes away mid-transfer, and it's the case
        this logging exists to make visible."""
        with caplog.at_level("INFO", logger="speech"):
            r = self.wrap([b"a" * 10, b"b" * 10], total=20)
            it = iter(r.response)
            next(it)
            it.close()
        assert "sent 10 of 20 bytes — INCOMPLETE" in "\n".join(caplog.messages)

    def test_the_file_behind_it_is_closed(self):
        """Replacing the body takes closing the file off werkzeug, so a transfer that ends —
        either way — has to close it here or the handle leaks per download."""
        class Body:
            closed = False
            def __iter__(self): return iter([b"x"])
            def close(self): self.closed = True

        body = Body()
        with core.app.test_request_context("/export/b1/A.m4b"):
            r = flask.Response(body, direct_passthrough=True)
            wrapped = core.log_transfer(r, "export A.m4b")
        list(wrapped.response)
        assert body.closed

    def test_the_file_is_closed_on_an_abandoned_transfer_too(self):
        class Body:
            closed = False
            def __iter__(self): return iter([b"x", b"y"])
            def close(self): self.closed = True

        body = Body()
        with core.app.test_request_context("/export/b1/A.m4b"):
            wrapped = core.log_transfer(flask.Response(body, direct_passthrough=True), "e")
        it = iter(wrapped.response)
        next(it)
        it.close()
        assert body.closed

    def test_names_the_request_it_was_serving(self, caplog):
        with caplog.at_level("INFO", logger="speech"):
            list(self.wrap([b"x"], total=1, what="export The Institute.m4b").response)
        assert "export The Institute.m4b" in "\n".join(caplog.messages)
