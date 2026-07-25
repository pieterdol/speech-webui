#!/usr/bin/env bash
# Publish the app on the tailnet over HTTPS. Only needs running once — tailscale
# persists the mapping across reboots.
#
# HTTPS is required, not cosmetic: Safari blocks getUserMedia (the microphone) and
# navigator.clipboard (the Copy button) outside a secure context. Port 8443 is used
# because 443 on this tailnet is already taken by another service.
set -e
tailscale serve --bg --https=8443 http://127.0.0.1:8600
echo
tailscale serve status
