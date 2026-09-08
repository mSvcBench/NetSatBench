#!/usr/bin/env bash
set -euo pipefail

ION_SOURCE_DIR="${1:?Usage: apply-ion-patches.sh <ION source directory>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$SCRIPT_DIR/patches"

test -d "$ION_SOURCE_DIR/.git" || {
  echo "ION source is not a Git checkout: $ION_SOURCE_DIR" >&2
  exit 1
}

for patch in "$PATCH_DIR"/*.patch; do
  if git -C "$ION_SOURCE_DIR" apply --reverse --check "$patch" 2>/dev/null; then
    echo "Already applied: $(basename "$patch")"
    continue
  fi
  git -C "$ION_SOURCE_DIR" apply --check "$patch"
  git -C "$ION_SOURCE_DIR" apply "$patch"
  echo "Applied: $(basename "$patch")"
done
