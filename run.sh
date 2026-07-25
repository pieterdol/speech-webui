#!/usr/bin/env bash
# Local Speech Studio. Listens on 127.0.0.1:8600; reach it from your phone at
# https://your-machine.your-tailnet.ts.net:8443 (see serve.sh).
cd "$(dirname "$0")"
exec .venv/bin/python speech.py
