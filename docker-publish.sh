#! /bin/bash
# Push the Maskroom image to the HMS registry.
# Mirrors /hms/apps/llm_router/docker-publish.sh.
#
# Requires HMS_REPO_WORKBENCH_REGISTRY_ROBOT_USER / _PWD in the environment.
#   ./docker-publish.sh              # tag = v<version> from pyproject.toml
#   TAG=v0.2.1 ./docker-publish.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_REGISTRY="repo.hsenidmobile.com"
DOCKER_IMAGE_DOMAIN="hms_data"
IMAGE_NAME="maskroom"
TAG=${TAG:-v$(sed -n 's/^version = "\(.*\)"/\1/p' "${HERE}/pyproject.toml")}
IMAGE="${CI_REGISTRY}/${DOCKER_IMAGE_DOMAIN}/${IMAGE_NAME}"

: "${HMS_REPO_WORKBENCH_REGISTRY_ROBOT_USER:?set the registry robot user}"
: "${HMS_REPO_WORKBENCH_REGISTRY_ROBOT_PWD:?set the registry robot password}"

# --password-stdin rather than -p, which docker warns is insecure and which
# leaves the password in shell history and the process list.
printf '%s' "${HMS_REPO_WORKBENCH_REGISTRY_ROBOT_PWD}" \
  | docker login "${CI_REGISTRY}" -u "${HMS_REPO_WORKBENCH_REGISTRY_ROBOT_USER}" --password-stdin

docker push "${IMAGE}:${TAG}"
docker push "${IMAGE}:latest"
echo "Published ${IMAGE}:${TAG} (and :latest)"
