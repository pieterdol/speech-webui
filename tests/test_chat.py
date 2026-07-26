"""Chat.

Ollama is never contacted — it's a separate server on :11434 and not this app's code. What is
this app's code is the transcript store, and the three-part arrangement that gets an English
answer out of a Dutch question: a note in the system prompt, a marker on the turn, and a
primer. That last part has a rule worth pinning — none of it may ever reach chats.json.
"""
import json
import urllib.error

import pytest

import chat


@pytest.fixture
def a_chat():
    def _make(chat_id="k1", **extra):
        item = {"id": chat_id, "name": "New chat", "model": "qwen3:8b",
                "system": "You are helpful.", "language": "en",
                "created": 0, "updated": 0, "messages": []}
        item.update(extra)
        chat.write_chats(chat.load_chats() + [item])
        return item
    return _make


class TestStore:
    def test_empty_and_corrupt(self):
        assert chat.load_chats() == []
        with open(chat.CHATS_FILE, "w") as f:
            f.write("not json")
        assert chat.load_chats() == []

    def test_find(self, a_chat):
        a_chat("k1")
        assert chat.find_chat("k1")["name"] == "New chat"
        assert chat.find_chat("nope") is None

    def test_summary_leaves_the_transcript_behind(self, a_chat):
        """The dropdown needs a label, not fifty turns of history."""
        a_chat("k1", messages=[{"role": "user", "content": "x" * 200},
                               {"role": "assistant", "content": "y" * 200}])
        s = chat.chat_summary(chat.find_chat("k1"))
        assert s["count"] == 2
        assert len(s["last"]) <= 80
        assert "messages" not in s

    def test_summary_last_is_the_latest_user_turn(self, a_chat):
        a_chat("k1", messages=[{"role": "user", "content": "first"},
                               {"role": "assistant", "content": "reply"},
                               {"role": "user", "content": "second"}])
        assert chat.chat_summary(chat.find_chat("k1"))["last"] == "second"


class TestAppendTurn:
    def test_stores_both_sides(self, a_chat):
        a_chat("k1")
        chat.append_turn("k1", "hello", "hi there", "qwen3:8b")
        msgs = chat.find_chat("k1")["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "hi there"
        assert msgs[1]["model"] == "qwen3:8b"

    def test_a_placeholder_name_becomes_the_first_question(self, a_chat):
        a_chat("k1", name="New chat")
        chat.append_turn("k1", "How do I bake bread?", "Slowly.", "qwen3:8b")
        assert chat.find_chat("k1")["name"] == "How do I bake bread?"

    def test_a_name_you_chose_is_kept(self, a_chat):
        a_chat("k1", name="Bread notes")
        chat.append_turn("k1", "Something else entirely", "ok", "qwen3:8b")
        assert chat.find_chat("k1")["name"] == "Bread notes"

    def test_the_name_is_one_line_and_bounded(self, a_chat):
        a_chat("k1", name="New chat")
        chat.append_turn("k1", "line one\nline two", "ok", "qwen3:8b")
        name = chat.find_chat("k1")["name"]
        assert "\n" not in name and len(name) <= 48

    def test_other_chats_are_untouched(self, a_chat):
        a_chat("k1")
        a_chat("k2")
        chat.append_turn("k1", "hello", "hi", "qwen3:8b")
        assert chat.find_chat("k2")["messages"] == []


class TestSystemPrompt:
    def test_just_the_persona_normally(self, a_chat):
        c = a_chat("k1", system="You are a pirate.")
        assert chat.chat_system(c, english_only=False) == "You are a pirate."

    def test_the_english_note_is_appended_when_it_applies(self, a_chat):
        c = a_chat("k1", system="You are a pirate.")
        out = chat.chat_system(c, english_only=True)
        assert out.startswith("You are a pirate.")
        assert chat.LANG_NOTE["nl"] in out

    def test_an_empty_persona_still_gets_the_note(self, a_chat):
        c = a_chat("k1", system="")
        assert chat.chat_system(c, english_only=True) == chat.LANG_NOTE["nl"]


class TestEnglishOnly:
    """The system prompt alone doesn't hold — qwen3:8b mirrors the language of the latest
    user turn. The marker and the primer are attached to what gets SENT, never to what's
    stored, so the transcript on screen and in chats.json stays clean."""

    def test_the_marker_and_primer_exist(self):
        assert chat.LANG_REMINDER["nl"].strip() == "[Reply in English.]"
        primer = chat.LANG_PRIMER["nl"]
        assert len(primer) == 4                      # two exchanges
        assert [m["role"] for m in primer] == ["user", "assistant", "user", "assistant"]

    def test_the_second_example_is_a_dutch_question_about_the_netherlands(self):
        """That's the specific failure mode: Dutch subject matter pulls it back to Dutch."""
        assert "Rotterdam" in chat.LANG_PRIMER["nl"][2]["content"]
        assert "Netherlands" in chat.LANG_PRIMER["nl"][3]["content"]

    def test_none_of_it_is_written_to_the_transcript(self, a_chat):
        a_chat("k1", language="nl")
        chat.append_turn("k1", "Hoe gaat het?", "I'm well, thanks!", "qwen3:8b")
        stored = json.dumps(chat.find_chat("k1"))
        assert "[Reply in English.]" not in stored
        assert "Rotterdam" not in stored


class TestOllamaError:
    """Turn a connection failure into the one instruction that fixes it."""

    def test_unreachable_says_how_to_start_it(self):
        msg = chat.ollama_error(urllib.error.URLError("refused"))
        assert "ollama serve" in msg

    def test_an_http_error_reports_what_it_said(self):
        e = urllib.error.HTTPError("u", 500, "boom", {}, None)
        assert isinstance(chat.ollama_error(e), str)

    def test_anything_else_is_truncated(self):
        assert len(chat.ollama_error(RuntimeError("x" * 900))) <= 300


class TestChatRoutes:
    def test_listing_is_summaries(self, client, a_chat):
        a_chat("k1", messages=[{"role": "user", "content": "hi"}])
        body = client.get("/api/chats").get_json()
        assert body["chats"][0]["count"] == 1
        assert "messages" not in body["chats"][0]

    def test_fetching_one_gives_the_whole_transcript(self, client, a_chat):
        a_chat("k1", messages=[{"role": "user", "content": "hi"}])
        body = client.get("/api/chats/k1").get_json()
        assert body["chat"]["messages"][0]["content"] == "hi"

    def test_unknown_chat(self, client):
        assert client.get("/api/chats/nope").status_code == 404

    def test_creating_one(self, client):
        body = client.post("/api/chats", json={"name": "Test"}).get_json()
        assert body["chat"]["id"]
        assert chat.find_chat(body["chat"]["id"])["name"] == "Test"

    def test_renaming(self, client, a_chat):
        a_chat("k1")
        client.post("/api/chats/update", json={"id": "k1", "name": "Renamed"})
        assert chat.find_chat("k1")["name"] == "Renamed"

    def test_deleting(self, client, a_chat):
        a_chat("k1")
        a_chat("k2")
        assert client.post("/api/chats/delete", json={"id": "k1"}).get_json()["ok"]
        assert chat.find_chat("k1") is None
        assert chat.find_chat("k2") is not None

    def test_clearing_keeps_the_chat(self, client, a_chat):
        a_chat("k1", messages=[{"role": "user", "content": "hi"}])
        client.post("/api/chats/clear", json={"id": "k1"})
        assert chat.find_chat("k1") is not None
        assert chat.find_chat("k1")["messages"] == []

    def test_delete_and_update_unknown(self, client):
        assert client.post("/api/chats/delete", json={"id": "nope"}).status_code == 404
        assert client.post("/api/chats/update", json={"id": "nope"}).status_code == 404

    def test_sending_an_empty_message(self, client, a_chat):
        a_chat("k1")
        assert client.post("/api/chat", json={"chat_id": "k1", "text": "  "}).status_code == 400

    def test_sending_to_an_unknown_chat(self, client):
        r = client.post("/api/chat", json={"chat_id": "nope", "text": "hello"})
        assert r.status_code == 404


class TestModelsRoute:
    def test_reports_the_failure_rather_than_crashing(self, client, monkeypatch):
        """Ollama being down is a normal state — it's started by hand."""
        def boom():
            raise urllib.error.URLError("refused")
        monkeypatch.setattr(chat, "ollama_models", boom)
        body = client.get("/api/models").get_json()
        assert body["ok"] is False
        assert "ollama serve" in body["error"]

    def test_lists_what_is_installed(self, client, monkeypatch):
        monkeypatch.setattr(chat, "ollama_models", lambda: ["qwen3:8b", "llama3:8b"])
        body = client.get("/api/models").get_json()
        assert body["ok"] is True
        assert body["models"] == ["qwen3:8b", "llama3:8b"]
