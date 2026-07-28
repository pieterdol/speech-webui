#!/usr/bin/env bash
# Install everything speech.py shells out to. Run once on a fresh machine, or to repair one
# engine after something breaks:
#
#   ./setup.sh                 # app venv + all three engines
#   ./setup.sh piper kokoro    # only those
#   ./setup.sh --force f5      # reinstall F5 even though it's already there
#   ./setup.sh --check         # report what's installed, change nothing
#
# Each engine keeps its own venv under ~/.local/share, and the app borrows their interpreters
# rather than duplicating their dependencies — see docs/architecture.md. Nothing here touches Ollama;
# that's `ollama.service` in this repo.
#
# Versions are pinned to what this app was tested against. Bumping them is fine, but F5 in
# particular is fussy about its torch build, so check it still renders afterwards.
set -euo pipefail

KOKORO_PKG="kokoro-onnx==0.5.0"
PIPER_PKG="piper-tts==1.6.0"
F5_PKG="f5-tts==1.1.22"
APP_PKGS="flask faster-whisper beautifulsoup4 lxml"
PYVER="3.12"

# Model files for Kokoro, from the kokoro-onnx release (sizes checked after download).
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES="kokoro-v1.0.onnx:325532387 voices-v1.0.bin:28214398"

# Every Dutch voice Piper ships, medium quality. The rest of its Dutch catalogue is lower
# quality versions of these same speakers, plus a 52-speaker model needing a speaker id the
# UI has no field for. Add a line here to install more; the app lists whatever is in the dir.
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICES="${PIPER_VOICES:-
nl/nl_NL/alex/medium/nl_NL-alex-medium.onnx
nl/nl_NL/pim/medium/nl_NL-pim-medium.onnx
nl/nl_NL/ronnie/medium/nl_NL-ronnie-medium.onnx
nl/nl_BE/nathalie/medium/nl_BE-nathalie-medium.onnx
nl/nl_BE/rdh/medium/nl_BE-rdh-medium.onnx
}"

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local/share}"     # overridable so this can be tested somewhere safe
BIN="${BIN:-$HOME/.local/bin}"
FORCE=0
CHECK=0
WANT=""

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    app|kokoro|piper|f5) WANT="$WANT $arg" ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done
[ -n "$WANT" ] || WANT="app kokoro piper f5"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '   \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
wants() { [[ " $WANT " == *" $1 "* ]]; }

command -v uv >/dev/null || die "uv is not installed — see https://docs.astral.sh/uv/"

# Download to a temp name and move into place, so an interrupted run can't leave a truncated
# file that later looks installed. $3, when given, is the exact size to expect.
fetch() {
  local url="$1" dest="$2" want="${3:-}"
  [ -f "$dest" ] && [ "$FORCE" = 0 ] && { ok "$(basename "$dest") already there"; return; }
  printf '   … %s\n' "$(basename "$dest")"
  # progress bar only when someone is watching; piped into a log it's unreadable noise
  local bar=(-s); [ -t 2 ] && bar=(--progress-bar)
  curl -fL --retry 3 "${bar[@]}" "$url" -o "$dest.part" || die "download failed: $url"
  if [ -n "$want" ]; then
    local got; got=$(stat -c%s "$dest.part")
    [ "$got" = "$want" ] || { rm -f "$dest.part"; die "$(basename "$dest"): got $got bytes, expected $want"; }
  fi
  mv "$dest.part" "$dest"
  ok "$(basename "$dest")"
}

venv_python() { echo "$PREFIX/$1/venv/bin/python"; }

make_venv() {
  local name="$1"
  local dir="$PREFIX/$name"
  if [ -x "$(venv_python "$name")" ] && [ "$FORCE" = 0 ]; then
    ok "venv exists"
    return 1          # caller skips installing into it
  fi
  [ "$FORCE" = 1 ] && rm -rf "$dir/venv"
  mkdir -p "$dir"
  uv venv --python "$PYVER" "$dir/venv" >/dev/null 2>&1 || die "could not create venv in $dir"
  ok "venv created"
  return 0
}

# ---- the app itself -------------------------------------------------------------------
if wants app && [ "$CHECK" = 0 ]; then
  say "app venv (flask + faster-whisper)"
  if [ -x "$HERE/.venv/bin/python" ] && [ "$FORCE" = 0 ]; then
    ok "already there"
  else
    [ "$FORCE" = 1 ] && rm -rf "$HERE/.venv"
    uv venv --python "$PYVER" "$HERE/.venv" >/dev/null 2>&1 || die "could not create .venv"
    # shellcheck disable=SC2086
    uv pip install --python "$HERE/.venv/bin/python" --quiet $APP_PKGS || die "pip install failed"
    ok "installed $APP_PKGS"
  fi
  warn "Whisper models download on first use (~500 MB small, ~1.6 GB turbo)"
fi

# ---- Kokoro: English TTS, ONNX ---------------------------------------------------------
if wants kokoro && [ "$CHECK" = 0 ]; then
  say "Kokoro (English TTS, ~350 MB of models)"
  if make_venv kokoro-tts; then
    uv pip install --python "$(venv_python kokoro-tts)" --quiet "$KOKORO_PKG" soundfile \
      || die "pip install failed"
    ok "installed $KOKORO_PKG"
  fi
  for spec in $KOKORO_FILES; do
    fetch "$KOKORO_BASE/${spec%%:*}" "$PREFIX/kokoro-tts/${spec%%:*}" "${spec##*:}"
  done
fi

# ---- Piper: Dutch TTS, ONNX -------------------------------------------------------------
if wants piper && [ "$CHECK" = 0 ]; then
  say "Piper (Dutch TTS, ~61 MB per voice)"
  if make_venv piper-tts; then
    uv pip install --python "$(venv_python piper-tts)" --quiet "$PIPER_PKG" || die "pip install failed"
    ok "installed $PIPER_PKG"
  fi
  mkdir -p "$PREFIX/piper-tts/voices"
  for path in $PIPER_VOICES; do
    base="$(basename "$path")"
    fetch "$PIPER_BASE/$path" "$PREFIX/piper-tts/voices/$base"
    fetch "$PIPER_BASE/$path.json" "$PREFIX/piper-tts/voices/$base.json"
  done
fi

# ---- F5-TTS: voice cloning ---------------------------------------------------------------
if wants f5 && [ "$CHECK" = 0 ]; then
  say "F5-TTS (voice cloning — several GB of torch, this takes a while)"
  if make_venv f5-tts; then
    # CPU torch on purpose: the default build would pull CUDA wheels this machine can't use,
    # and F5 runs on the cores here anyway.
    uv pip install --python "$(venv_python f5-tts)" --torch-backend=cpu --quiet "$F5_PKG" \
      || die "pip install failed"
    ok "installed $F5_PKG (cpu torch)"
  fi
  # speech.py invokes this wrapper, not the venv directly
  mkdir -p "$BIN"
  if [ ! -x "$BIN/f5-tts" ] || [ "$FORCE" = 1 ]; then
    printf '#!/bin/bash\nexec "%s/f5-tts/venv/bin/f5-tts_infer-cli" "$@"\n' "$PREFIX" > "$BIN/f5-tts"
    chmod +x "$BIN/f5-tts"
    ok "wrote $BIN/f5-tts"
  else
    ok "$BIN/f5-tts already there"
  fi
  warn "F5 downloads its own model on first render (~1.4 GB, into ~/.cache/huggingface)"
fi

# ---- what actually works now --------------------------------------------------------------
say "checking"
probe() {   # name, python, import statement, description
  local py="$2"
  if [ ! -x "$py" ]; then warn "$1: not installed"; return; fi
  if "$py" -c "$3" >/dev/null 2>&1; then ok "$1: $4"; else warn "$1: venv exists but $3 fails"; fi
}
probe "app venv" "$HERE/.venv/bin/python" "import flask, faster_whisper" "flask + faster-whisper"
probe "kokoro"   "$(venv_python kokoro-tts)" "import kokoro_onnx, soundfile" "kokoro-onnx"
probe "piper"    "$(venv_python piper-tts)"  "import piper" "piper-tts"
probe "f5"       "$(venv_python f5-tts)"     "import f5_tts" "f5-tts"

for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [ -f "$PREFIX/kokoro-tts/$f" ] && ok "kokoro model $f" || warn "kokoro model $f missing"
done
n=$(ls "$PREFIX/piper-tts/voices/"*.onnx 2>/dev/null | wc -l)
[ "$n" -gt 0 ] && ok "piper voices: $n" || warn "no piper voices in $PREFIX/piper-tts/voices"
[ -x "$BIN/f5-tts" ] && ok "f5-tts CLI on PATH at $BIN/f5-tts" || warn "$BIN/f5-tts missing"

echo
echo "Start it with ./run.sh (or ./restart.sh to background it)."
