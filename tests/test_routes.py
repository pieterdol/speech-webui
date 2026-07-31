"""The URL table, written out in full.

Routes are registered by importing the feature modules for their side effect, which means a
module that stops being imported takes its endpoints with it and nothing else notices. This
is the list as it stands; a diff here is either a deliberate change or a module that fell out
of the import chain during a refactor.
"""
import speech

EXPECTED = {
    ("/", "GET"),
    ("/api/books", "GET"),
    ("/api/books", "POST"),
    ("/api/books/<book_id>", "GET"),
    ("/api/books/<book_id>/cast/<int:index>", "GET"),
    ("/api/books/<book_id>/find", "GET"),
    ("/api/books/<book_id>/preview/<int:index>", "GET"),
    ("/api/books/<book_id>/skipped/<int:n>", "GET"),
    ("/api/books/cast", "POST"),
    ("/api/books/cast/voice", "POST"),
    ("/api/books/clear", "POST"),
    ("/api/books/cover", "POST"),
    ("/api/books/delete", "POST"),
    ("/api/books/describe", "POST"),
    ("/api/books/export", "POST"),
    ("/api/books/export/delete", "POST"),
    ("/api/books/insert", "POST"),
    ("/api/books/render", "POST"),
    ("/api/books/render_cancel", "POST"),
    ("/api/books/respell", "POST"),
    ("/api/books/retry", "POST"),
    ("/api/books/render_all", "POST"),
    ("/api/books/render_stop", "POST"),
    ("/api/books/rescan", "POST"),
    ("/api/books/skip", "POST"),
    ("/api/books/update", "POST"),
    ("/api/chat", "POST"),
    ("/api/chats", "GET"),
    ("/api/chats", "POST"),
    ("/api/chats/<chat_id>", "GET"),
    ("/api/chats/clear", "POST"),
    ("/api/chats/delete", "POST"),
    ("/api/chats/update", "POST"),
    ("/api/clips", "GET"),
    ("/api/clips", "POST"),
    ("/api/clips/delete", "POST"),
    ("/api/dictate", "POST"),
    ("/api/models", "GET"),
    ("/api/out/delete", "POST"),
    ("/api/presets", "GET"),
    ("/api/presets", "POST"),
    ("/api/presets/delete", "POST"),
    ("/api/sample/<voice>", "GET"),
    ("/api/say", "GET"),
    ("/api/speak", "POST"),
    ("/api/status/<job_id>", "GET"),
    ("/api/transcribe", "POST"),
    ("/api/voices", "GET"),
    ("/book/<book_id>/<path:filename>", "GET"),
    ("/clip/<path:filename>", "GET"),
    ("/cover/<book_id>/<size>.jpg", "GET"),
    ("/export/<book_id>/<path:filename>", "GET"),
    ("/favicon.ico", "GET"),
    ("/get/<book_id>/<path:filename>", "GET"),
    ("/icon.png", "GET"),
    ("/out/<path:filename>", "GET"),
    ("/preset/<path:filename>", "GET"),
    ("/static/<path:filename>", "GET"),      # Flask's own
}


def actual():
    out = set()
    for rule in speech.app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out.add((str(rule), method))
    return out


def test_no_route_went_missing():
    assert sorted(EXPECTED - actual()) == []


def test_no_route_appeared_unannounced():
    assert sorted(actual() - EXPECTED) == []


def test_every_feature_module_registered_something():
    """Each module owns part of the table. An empty one means it wasn't imported."""
    owned = {
        "books": "/api/books",
        "chat": "/api/chat",
        "clips": "/api/clips",
        "stt": "/api/transcribe",
        "tts": "/api/speak",
    }
    paths = {p for p, _m in actual()}
    for module, path in owned.items():
        assert path in paths, f"{module} registered nothing"
