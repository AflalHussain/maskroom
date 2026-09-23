#!/usr/bin/env bash
# Copy this checkout to the server and run the installer there (the server has no git).
#   deploy/aws/ship.sh -i ~/keys/server.pem sovereign@<server-ip-or-host>   # copy + install
#   deploy/aws/ship.sh -i ~/keys/server.pem sovereign@<server> --no-install # copy only
# The key can also be given as SSH_KEY=/path/to.pem. REMOTE_DIR overrides the target dir.
set -euo pipefail
SSH_KEY=${SSH_KEY:-}
if [ "${1:-}" = "-i" ]; then SSH_KEY=${2:?path to the .pem file}; shift 2; fi
TARGET=${1:?usage: ship.sh [-i key.pem] user@host [--no-install]}
REMOTE_DIR=${REMOTE_DIR:-/hms/apps/masking}
SSH_CMD="ssh -o StrictHostKeyChecking=accept-new"
if [ -n "$SSH_KEY" ]; then
  [ -r "$SSH_KEY" ] || { echo "cannot read key: $SSH_KEY"; exit 1; }
  chmod 600 "$SSH_KEY" 2>/dev/null || true       # ssh refuses world-readable keys
  SSH_CMD="$SSH_CMD -i $SSH_KEY"
fi
cd "$(dirname "$0")/../.."
echo "==> rsync -> $TARGET:$REMOTE_DIR"
$SSH_CMD "$TARGET" "mkdir -p $REMOTE_DIR"
rsync -az --delete -e "$SSH_CMD" \
  --exclude pii_env/ --exclude .git/ --exclude data/ --exclude webui/runs/ --exclude local/ \
  --exclude model_training/ --exclude __pycache__/ --exclude '*.pyc' --exclude .pytest_cache/ \
  --exclude .env --exclude 'deploy/aws/nginx/maskroom.conf' --exclude '*.db' --exclude tests/data/ \
  ./ "$TARGET:$REMOTE_DIR/"
if [ "${2:-}" != "--no-install" ]; then
  echo "==> running install.sh on $TARGET"
  $SSH_CMD -t "$TARGET" "cd $REMOTE_DIR && deploy/aws/install.sh"
fi
