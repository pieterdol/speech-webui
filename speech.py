#!/usr/bin/env python3
"""
speech-webui — a phone-friendly web front-end for the local speech tools.

  speech → text   faster-whisper (in-process, model kept resident)
  text → speech   Kokoro (54 built-in voices, seconds) or F5-TTS (clones a clip, minutes)
  chat            Ollama (Qwen et al. on the GPU), with replies spoken by Kokoro

Both TTS engines keep their own venvs so this app doesn't duplicate their dependencies.
F5-TTS is invoked as a CLI per render (it's minutes of work anyway); Kokoro instead runs
as a resident worker started with ITS interpreter — see kokoro_worker.py — because the
per-invocation model load was costing more than the audio on short text. Ollama is a
separate server we talk to over HTTP on :11434.

Reach it from your phone over Tailscale at https://your-machine.your-tailnet.ts.net:8443
(HTTPS is required — Safari blocks the microphone and the clipboard on plain http).
"""
import os

from flask import Response, send_from_directory

from core import HERE, PORT, app
# Imported for their side effect: each module registers its routes on the shared app.
import books, chat, clips, stt, tts     # noqa: F401

@app.get("/icon.png")
@app.get("/favicon.ico")
def icon():
    return send_from_directory(HERE, "icon.png", mimetype="image/png")

@app.get("/")
def index():
    # re-read each time + no-store so home-screen shortcuts always get the latest UI
    page = open(os.path.join(HERE, "index.html")).read()
    r = Response(page, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r


if __name__ == "__main__":
    books.clear_stale_state()
    if tts.TTS_IDLE_SECONDS > 0:
        import threading
        threading.Thread(target=tts.worker_reaper, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
