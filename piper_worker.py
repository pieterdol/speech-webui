#!/usr/bin/env python3
"""Long-lived Piper process — Dutch text to speech, driven over stdin/stdout by speech.py.

Kokoro has no Dutch voice; Piper does, and they're small (~61 MB ONNX each) and quick. Same
arrangement as kokoro_worker.py: run with Piper's OWN interpreter so its dependencies stay in
their venv, keep the model loaded between requests, one JSON request per line.

    ~/.local/share/piper-tts/venv/bin/python piper_worker.py [voices_dir]

    {"op":"say","text":"…","voice":"nl_NL-ronnie-medium","speed":1.0,"out":"/tmp/x.wav"}
      voice can name a speaker inside a multi-speaker model: "nl_NL-mls-medium-2450".
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

# speech.py always passes the directory; the fallback is for running this by hand.
VOICES_DIR = Path(sys.argv[1] if len(sys.argv) > 1
                  else Path.home() / ".local/share/piper-tts/voices")

# Same trick as the Kokoro worker: keep the protocol channel clear of library chatter.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)

_loaded = {}


def reply(obj):
    _proto.write(json.dumps(obj) + "\n")


def speakers_of(model):
    """{label: speaker_id} for a multi-speaker model, {} for an ordinary one.

    nl_NL-mls-medium is one 76 MB model holding 52 readers, which is the whole reason to have
    it: Dutch has no high-quality Piper voice, so the way to a better one is picking among
    speakers. The labels are the model's own — MLS numbers its readers "2450", "1724" — and
    they're what a voice id names, rather than the position in the map.
    """
    try:
        cfg = json.loads(Path(str(model) + ".json").read_text())
    except (OSError, ValueError):
        return {}
    if int(cfg.get("num_speakers") or 1) < 2:
        return {}
    return {str(k): int(v) for k, v in (cfg.get("speaker_id_map") or {}).items()}


def list_voices():
    """Whatever .onnx files are sitting in the voices dir, newest naming scheme:
    nl_NL-ronnie-medium.onnx -> lang nl_NL, name ronnie, quality medium. A multi-speaker model
    is listed once per speaker, so the rest of the app goes on treating a voice as one string."""
    out = []
    for p in sorted(VOICES_DIR.glob("*.onnx")):
        vid = p.stem
        bits = vid.split("-")
        base = {"lang": bits[0] if bits else vid,
                "name": bits[1] if len(bits) > 1 else vid,
                "quality": bits[2] if len(bits) > 2 else ""}
        speakers = speakers_of(p)
        if not speakers:
            out.append({"id": vid, **base})
            continue
        for label in sorted(speakers, key=lambda k: speakers[k]):
            out.append({**base, "id": f"{vid}-{label}", "name": f"{base['name']} {label}"})
    return out


def resolve(vid):
    """(model file, speaker id or None) for a voice id — which may name a speaker inside a
    multi-speaker model, "nl_NL-mls-medium-2450"."""
    path = VOICES_DIR / f"{vid}.onnx"
    if path.exists():
        return path, None
    base, _, label = vid.rpartition("-")
    path = VOICES_DIR / f"{base}.onnx"
    if base and path.exists():
        speaker = speakers_of(path).get(label)
        if speaker is not None:
            return path, speaker
    raise FileNotFoundError(f"no such voice: {vid}")


def get_voice(vid):
    """The loaded model and the speaker to use. Keyed by file, so all 52 speakers of a
    multi-speaker model share the one 76 MB model rather than loading it each."""
    from piper import PiperVoice
    path, speaker = resolve(vid)
    key = str(path)
    if key not in _loaded:
        _loaded[key] = PiperVoice.load(key)
    return _loaded[key], speaker


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
                voice, speaker = get_voice(req.get("voice") or "")
                speed = float(req.get("speed") or 1.0)
                # length_scale stretches time, so it's the reciprocal of a speed multiplier
                cfg = SynthesisConfig(length_scale=(1.0 / speed) if speed else 1.0,
                                      speaker_id=speaker)
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
