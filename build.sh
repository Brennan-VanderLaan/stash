#!/usr/bin/env bash
# Build the Stash docker image with consistent tags:
#   stash:latest          — always updated
#   stash:<git-short-sha> — pinned to the current commit (suffixed -dirty if worktree has changes)
#
# Usage: ./build.sh [additional docker build args...]

set -euo pipefail

cd "$(dirname "$0")"

IMAGE_NAME="${STASH_IMAGE:-stash}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "error: build.sh must be run from within the git repo" >&2
    exit 1
fi

SHA="$(git rev-parse --short HEAD)"
if ! git diff --quiet HEAD -- . || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    SHA="${SHA}-dirty"
fi

echo "==> Building ${IMAGE_NAME}:${SHA} (and :latest)"
docker build \
    --pull \
    -t "${IMAGE_NAME}:${SHA}" \
    -t "${IMAGE_NAME}:latest" \
    "$@" \
    .

echo
echo "Built:"
echo "  ${IMAGE_NAME}:${SHA}"
echo "  ${IMAGE_NAME}:latest"
