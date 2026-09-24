#!/usr/bin/env bash
# Deploy Maskroom to the server. Default: publish the image to the HMS registry
# (docker-build.sh + docker-publish.sh), copy only the deploy files, run install.sh
# there, which pulls the image. The server never sees the source and never builds.
#
#   deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server>             # build, publish, deploy
#   deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server> --no-install
#   deploy/aws/ship.sh --load -i ~/keys/server.pem ec2-user@<server>      # no registry: stream the
#                                                                          # image over SSH instead
# Env: SSH_KEY (instead of -i), REMOTE_DIR (default /hms/apps/masking), TAG (default
#      v<version> from pyproject.toml), SKIP_BUILD=1 (reuse the local image), SKIP_PUBLISH=1,
#      HMS_REPO_WORKBENCH_REGISTRY_ROBOT_USER/_PWD for the publish step.
set -euo pipefail
MODE=registry
SSH_KEY=${SSH_KEY:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --load) MODE=load; shift;;
    -i) SSH_KEY=${2:?path to the .pem file}; shift 2;;
    *) break;;
  esac
done
TARGET=${1:?usage: ship.sh [--load] [-i key.pem] user@host [--no-install]}
REMOTE_DIR=${REMOTE_DIR:-/hms/apps/masking}
cd "$(dirname "$0")/../.."
TAG=${TAG:-v$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)}
IMAGE="repo.hsenidmobile.com/hms_data/maskroom:$TAG"
SSH_CMD="ssh -o StrictHostKeyChecking=accept-new"
if [ -n "$SSH_KEY" ]; then
  [ -r "$SSH_KEY" ] || { echo "cannot read key: $SSH_KEY"; exit 1; }
  chmod 600 "$SSH_KEY" 2>/dev/null || true
  SSH_CMD="$SSH_CMD -i $SSH_KEY"
fi

[ "${SKIP_BUILD:-0}" = "1" ] || TAG=$TAG ./docker-build.sh

$SSH_CMD "$TARGET" "mkdir -p $REMOTE_DIR/nginx $REMOTE_DIR/keycloak"
if [ "$MODE" = registry ]; then
  [ "${SKIP_PUBLISH:-0}" = "1" ] || TAG=$TAG ./docker-publish.sh
else
  echo "==> streaming $IMAGE to $TARGET over SSH (docker save | docker load; compressed in flight)"
  if $SSH_CMD "$TARGET" "docker image inspect $IMAGE >/dev/null 2>&1"; then
    echo "    image already present on the server, skipping transfer"
  elif command -v pv >/dev/null; then
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
