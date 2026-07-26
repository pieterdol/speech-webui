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

# speech.py always passes the directory; the fallback is for running this by hand.
MODEL_DIR = Path(sys.argv[1] if len(sys.argv) > 1
                 else Path.home() / ".local/share/kokoro-tts")

_proto = None       # the real stdout, set aside by main()


def reply(obj):
    _proto.write(json.dumps(obj) + "\n")


# Kokoro reads 510 phonemes at a time. kokoro-onnx batches longer input itself, but its
# splitter only breaks at punctuation, so a stretch that has none comes back as a single
# oversized batch — and the model then indexes the voice's style table by the token count:
# exactly 510 raises "index 510 is out of bounds for axis 0 with size 510", above that it
# refuses outright. A page of book titles, one per line, is precisely that shape.
#
# So the split happens here instead, before kokoro-onnx sees it. English runs about 1.1
# phonemes per character and money and dates far more — "$100,000" is thirty — so no limit on
# the text can stand in for this one; it has to be counted in phonemes.
MAX_PHONEMES = 500      # a little under 510: what's counted is the batch, and slack is free


def phoneme_batches(phonemes, limit=MAX_PHONEMES):
    """Cut a phoneme string into pieces the model will take, at the latest boundary that fits.

    Punctuation first, since that's where a voice would draw breath anyway, then a space, then
    mid-word as a last resort — a piece that can't be cut cleanly is still better than one the
    model won't read at all.
    """
    out, rest = [], phonemes.strip()
    while len(rest) > limit:
        head = rest[:limit]
        mark = max((head.rfind(c) for c in ".,!?;:"), default=-1)
        cut = mark + 1 if mark >= 0 else head.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def main():
    global _proto
    # Keep the protocol channel clean: onnxruntime and friends write banners and warnings to
    # stdout, which would be parsed as responses. Dup the real stdout aside for our own use and
    # point fd 1 at stderr, so any library chatter lands in speech.log instead. Before the
    # imports below, which is where the banners come from.
    _proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)

    import numpy as np
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
                lang = req.get("lang") or "en-us"
                batches = phoneme_batches(kokoro.tokenizer.phonemize(text, lang))
                if not batches:
                    reply({"ok": False, "error": "nothing to say in that text"})
                    continue
                # is_phonemes, so the text is phonemized once and the batching above is what
                # decides where the model's reading is broken.
                clips = [kokoro.create(b, voice=req.get("voice") or "af_heart",
                                       speed=float(req.get("speed") or 1.0),
                                       lang=lang, is_phonemes=True)
                         for b in batches]
                samples = np.concatenate([c[0] for c in clips])
                rate = clips[0][1]
                sf.write(req["out"], samples, rate)
                reply({"ok": True, "seconds": round(len(samples) / rate, 1)})
            else:
                reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
