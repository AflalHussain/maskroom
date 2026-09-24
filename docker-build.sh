#! /bin/bash
# Build the Maskroom image and tag it for the HMS registry.
# Mirrors /hms/apps/llm_router/docker-build.sh (same registry and namespace).
#
#   ./docker-build.sh              # tag = v<version> from pyproject.toml
#   TAG=v0.2.1 ./docker-build.sh   # explicit tag
#   NO_CACHE=1 ./docker-build.sh   # full rebuild (refreshes the OS package layer)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_REGISTRY="repo.hsenidmobile.com"
DOCKER_IMAGE_DOMAIN="hms_data"
IMAGE_NAME="maskroom"
TAG=${TAG:-v$(sed -n 's/^version = "\(.*\)"/\1/p' "${HERE}/pyproject.toml")}
IMAGE="${CI_REGISTRY}/${DOCKER_IMAGE_DOMAIN}/${IMAGE_NAME}"

docker build ${NO_CACHE:+--no-cache} -t "${IMAGE}:${TAG}" "${HERE}" &&
docker tag "${IMAGE}:${TAG}" "${IMAGE}:latest"

echo
echo "Built:"
docker images --format "  {{.Repository}}:{{.Tag}}  {{.Size}}" | grep "${DOCKER_IMAGE_DOMAIN}/${IMAGE_NAME}"
