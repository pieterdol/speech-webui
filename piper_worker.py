#!/usr/bin/env python3
"""Long-lived Piper process — Dutch text to speech, driven over stdin/stdout by speech.py.

Kokoro has no Dutch voice; Piper does, and they're small (~61 MB ONNX each) and quick. Same
arrangement as kokoro_worker.py: run with Piper's OWN interpreter so its dependencies stay in
their venv, keep the model loaded between requests, one JSON request per line.

    ~/.local/share/piper-tts/venv/bin/python piper_worker.py [voices_dir]

    {"op":"say","text":"…","voice":"nl_NL-ronnie-medium","speed":1.0,"out":"/tmp/x.wav"}
      -> {"ok":true,"seconds":4.2}
    {"op":"voices"}   -> {"ok":true,"voices":[{"id":…,"lang":…,"name":…}, …]}
    {"op":"ping"}     -> {"ok":true}

Voices are loaded on first use and then kept — one 61 MB model per voice actually used, not
all of them up front.
"""
import json
import os
import sys
import wave
from pathlib import Path

VOICES_DIR = Path(sys.argv[1] if len(sys.argv) > 1
                  else "/home/USER/.local/share/piper-tts/voices")

# Same trick as the Kokoro worker: keep the protocol channel clear of library chatter.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)

_loaded = {}


def reply(obj):
    _proto.write(json.dumps(obj) + "\n")


def list_voices():
    """Whatever .onnx files are sitting in the voices dir, newest naming scheme:
    nl_NL-ronnie-medium.onnx -> lang nl_NL, name ronnie, quality medium."""
    out = []
    for p in sorted(VOICES_DIR.glob("*.onnx")):
        vid = p.stem
        bits = vid.split("-")
        out.append({"id": vid,
                    "lang": bits[0] if bits else vid,
                    "name": bits[1] if len(bits) > 1 else vid,
                    "quality": bits[2] if len(bits) > 2 else ""})
    return out


def get_voice(vid):
    from piper import PiperVoice
    if vid not in _loaded:
        path = VOICES_DIR / f"{vid}.onnx"
        if not path.exists():
            raise FileNotFoundError(f"no such voice: {vid}")
        _loaded[vid] = PiperVoice.load(str(path))
    return _loaded[vid]


def main():
    from piper import SynthesisConfig
    reply({"ok": True, "ready": True})       # speech.py waits for this before sending work

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
                reply({"ok": True, "voices": list_voices()})
            elif op == "say":
                text = (req.get("text") or "").strip()
                if not text:
                    reply({"ok": False, "error": "no input text"})
                    continue
                voice = get_voice(req.get("voice") or "")
                speed = float(req.get("speed") or 1.0)
                # length_scale stretches time, so it's the reciprocal of a speed multiplier
                cfg = SynthesisConfig(length_scale=(1.0 / speed) if speed else 1.0)
                with wave.open(req["out"], "wb") as wav:
                    voice.synthesize_wav(text, wav, syn_config=cfg)
                with wave.open(req["out"], "rb") as wav:
                    seconds = round(wav.getnframes() / float(wav.getframerate()), 1)
                reply({"ok": True, "seconds": seconds})
            else:
                reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
