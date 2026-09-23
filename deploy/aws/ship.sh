#!/usr/bin/env bash
# Copy this checkout to the server and run the installer there (the server has no git).
#   deploy/aws/ship.sh sovereign@<server-ip-or-host>            # copy + install
#   deploy/aws/ship.sh sovereign@<server> --no-install          # copy only
set -euo pipefail
TARGET=${1:?usage: ship.sh user@host [--no-install]}
REMOTE_DIR=${REMOTE_DIR:-/hms/apps/sovereign-ai/masking}
cd "$(dirname "$0")/../.."
echo "==> rsync -> $TARGET:$REMOTE_DIR"
rsync -az --delete \
  --exclude pii_env/ --exclude .git/ --exclude data/ --exclude webui/runs/ --exclude local/ \
  --exclude model_training/ --exclude __pycache__/ --exclude '*.pyc' --exclude .pytest_cache/ \
  --exclude .env --exclude 'deploy/aws/nginx/maskroom.conf' --exclude '*.db' --exclude tests/data/ \
  ./ "$TARGET:$REMOTE_DIR/"
if [ "${2:-}" != "--no-install" ]; then
  echo "==> running install.sh on $TARGET"
  ssh -t "$TARGET" "cd $REMOTE_DIR && deploy/aws/install.sh"
fi
