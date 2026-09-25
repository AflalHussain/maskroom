#!/bin/bash
# Serve this desktop/ folder to the Windows dev PC over the LAN, so
# desktop/dev-sync.ps1 there can pull every change and restart the helper.
#
#   ./desktop/dev-serve.sh            # http://<this machine>:8765/
#   PORT=9000 ./desktop/dev-serve.sh
#
# Dev-only: plain HTTP, no auth, serves only this folder (never the repo root,
# which holds certs/ and .env). Stop it with Ctrl+C when you are done.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=${PORT:-8765}
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Serving ${HERE} at http://${IP:-<this-ip>}:${PORT}/  (Ctrl+C to stop)"
echo "On the Windows PC:  powershell -ExecutionPolicy Bypass -File dev-sync.ps1 -Source http://${IP:-<this-ip>}:${PORT}"
exec python3 -m http.server "${PORT}" --bind 0.0.0.0 --directory "${HERE}"
