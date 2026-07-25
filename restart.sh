#!/usr/bin/env bash
# Reliably restart the app so it picks up speech.py changes.
# Matches on "speech.py" specifically — comfy-webui's restart.sh matches "app.py",
# so keeping the entrypoints differently named stops the two from killing each other.
cd "$(dirname "$0")"
for p in $(pgrep -f 'speech\.py'); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "python" ] && kill "$p"
done
sleep 2
for p in $(pgrep -f 'speech\.py'); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "python" ] && kill -9 "$p"
done
sleep 1
setsid .venv/bin/python speech.py >speech.log 2>&1 < /dev/null &
sleep 3
if ss -ltn 2>/dev/null | grep -q 127.0.0.1:8600; then
  echo "✅ speech studio restarted — listening on 127.0.0.1:8600"
else
  echo "❌ it did not come up; check ~/Code/speech-webui/speech.log"
fi
