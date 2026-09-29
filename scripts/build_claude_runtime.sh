#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_IMAGE="${PAIRWISE_CLAUDE_BASE_IMAGE:-claude-eval-runtime:claude-2.1.269}"
TARGET_IMAGE="${PAIRWISE_CLAUDE_IMAGE:-claude-eval-runtime:claude-2.1.269-tools}"
IMAGE_MODE="${PAIRWISE_CLAUDE_IMAGE_MODE:-}"
if [[ -z "$IMAGE_MODE" ]]; then
  # Existing installations already point at the complete -tools image.
  IMAGE_MODE="build"
  if [[ "$TARGET_IMAGE" == "claude-eval-runtime:claude-2.1.269-tools" ]]; then
    IMAGE_MODE="prebuilt"
  fi
fi

case "$IMAGE_MODE" in
  prebuilt)
    if ! docker image inspect "$TARGET_IMAGE" >/dev/null 2>&1; then
      echo "Prebuilt Claude image is missing: $TARGET_IMAGE. Load the transferred image before installation." >&2
      exit 1
    fi
    ;;
  build)
    if [[ "$BASE_IMAGE" == "$TARGET_IMAGE" ]]; then
      echo "Base and prepared Claude image tags must be different." >&2
      exit 1
    fi
    docker image inspect "$BASE_IMAGE" >/dev/null
    docker build \
      --build-arg "BASE_IMAGE=$BASE_IMAGE" \
      --file "$ROOT/docker/ClaudeRuntime.Dockerfile" \
      --tag "$TARGET_IMAGE" \
      "$ROOT/docker"
    ;;
  *)
    echo "Unknown PAIRWISE_CLAUDE_IMAGE_MODE: $IMAGE_MODE (expected prebuilt or build)." >&2
    exit 1
    ;;
esac

docker run --rm --entrypoint /bin/sh "$TARGET_IMAGE" -lc '
  set -eu
  claude --version
  python3 -m pip --version
  python3 -m venv /tmp/pairwise-venv
  /tmp/pairwise-venv/bin/python -m pip --version
  go version
  node --version
  npm --version
  pnpm --version
  tsc --version
  tsx --version
  vite --version
  vitest --version
'
echo "Validated Claude image ($IMAGE_MODE): $TARGET_IMAGE"
