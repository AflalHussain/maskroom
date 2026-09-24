#!/usr/bin/env bash
# Build the Maskroom image HERE, push it to the server over SSH, copy only the deploy
# files, and run the installer there. The server never sees the source and never builds.
#   deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server>            # build + ship + install
#   deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server> --no-install
# Env: SSH_KEY (instead of -i), REMOTE_DIR (default /hms/apps/masking),
#      MASKROOM_TAG (default: git short sha), SKIP_BUILD=1 to reuse the local image.
set -euo pipefail
SSH_KEY=${SSH_KEY:-}
if [ "${1:-}" = "-i" ]; then SSH_KEY=${2:?path to the .pem file}; shift 2; fi
TARGET=${1:?usage: ship.sh [-i key.pem] user@host [--no-install]}
REMOTE_DIR=${REMOTE_DIR:-/hms/apps/masking}
cd "$(dirname "$0")/../.."
TAG=${MASKROOM_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}
IMAGE="maskroom:$TAG"
SSH_CMD="ssh -o StrictHostKeyChecking=accept-new"
if [ -n "$SSH_KEY" ]; then
  [ -r "$SSH_KEY" ] || { echo "cannot read key: $SSH_KEY"; exit 1; }
  chmod 600 "$SSH_KEY" 2>/dev/null || true
  SSH_CMD="$SSH_CMD -i $SSH_KEY"
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "==> docker build -t $IMAGE ."
  docker build -t "$IMAGE" .
fi

echo "==> shipping $IMAGE to $TARGET (docker save | ssh docker load; ~2 GB, compressed in flight)"
$SSH_CMD "$TARGET" "mkdir -p $REMOTE_DIR/nginx $REMOTE_DIR/keycloak"
if $SSH_CMD "$TARGET" "docker image inspect $IMAGE >/dev/null 2>&1"; then
  echo "    image already present on the server, skipping transfer"
else
  if command -v pv >/dev/null; then
    docker save "$IMAGE" | gzip -1 | pv -brt | $SSH_CMD "$TARGET" "gunzip | docker load"
  else
    docker save "$IMAGE" | gzip -1 | dd bs=4M status=progress | $SSH_CMD "$TARGET" "gunzip | docker load"
  fi
fi

echo "==> copying deploy files -> $TARGET:$REMOTE_DIR"
rsync -az -e "$SSH_CMD" deploy/aws/docker-compose.server.yml deploy/aws/install.sh "$TARGET:$REMOTE_DIR/"
rsync -az -e "$SSH_CMD" deploy/aws/nginx/maskroom.conf.template "$TARGET:$REMOTE_DIR/nginx/"
rsync -az -e "$SSH_CMD" deploy/keycloak/realm-maskroom.json "$TARGET:$REMOTE_DIR/keycloak/"
$SSH_CMD "$TARGET" "chmod +x $REMOTE_DIR/install.sh"

if [ "${2:-}" != "--no-install" ]; then
  echo "==> running install.sh on $TARGET"
  $SSH_CMD -t "$TARGET" "cd $REMOTE_DIR && MASKROOM_IMAGE=$IMAGE ./install.sh"
fi
