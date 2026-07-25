#!/usr/bin/env python3
"""Long-lived Kokoro process, driven over stdin/stdout by speech.py.

The kokoro-tts CLI is one-shot: it loads the 325 MB ONNX model on every invocation, which
measured at ~1.9 s of fixed cost per render regardless of how short the text is. That's most
of the latency on a one-sentence reply, and it's what makes speaking a chat reply sentence
by sentence impractical. Here the model is loaded once and stays put.

Run with Kokoro's own interpreter, NOT the app venv — that keeps the ~524 MB of ONNX
dependencies where they already are instead of duplicating them:

    ~/.local/share/kokoro-tts/venv/bin/python kokoro_worker.py [model_dir]

Protocol: one JSON request per line in, one JSON response per line out.

    {"op":"say","text":"…","voice":"af_heart","speed":1.0,"lang":"en-us","out":"/tmp/x.wav"}
      -> {"ok":true,"seconds":4.2}
    {"op":"voices"}   -> {"ok":true,"voices":["af_alloy", …]}
    {"op":"ping"}     -> {"ok":true}

Errors come back as {"ok":false,"error":"…"} rather than killing the process, so one bad
request doesn't cost the caller a reload of the model.
"""
import json
import os
import sys
from pathlib import Path

MODEL_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/USER/.local/share/kokoro-tts")

# Keep the protocol channel clean: onnxruntime and friends write banners and warnings to
# stdout, which would be parsed as responses. Dup the real stdout aside for our own use and
# point fd 1 at stderr, so any library chatter lands in speech.log instead.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)


def reply(obj):
    _proto.write(json.dumps(obj) + "\n")


def main():
    import soundfile as sf
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(MODEL_DIR / "kokoro-v1.0.onnx"), str(MODEL_DIR / "voices-v1.0.bin"))
    reply({"ok": True, "ready": True})          # speech.py waits for this before sending work

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op", "say")
            if op == "ping":
                reply({"ok": True})
            elif op == "voices":
                reply({"ok": True, "voices": sorted(kokoro.get_voices())})
            elif op == "say":
                text = (req.get("text") or "").strip()
                if not text:
                    reply({"ok": False, "error": "no input text"})
                    continue
                samples, rate = kokoro.create(text, voice=req.get("voice") or "af_heart",
                                              speed=float(req.get("speed") or 1.0),
                                              lang=req.get("lang") or "en-us")
                sf.write(req["out"], samples, rate)
                reply({"ok": True, "seconds": round(len(samples) / rate, 1)})
            else:
                reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
