#!/usr/bin/env bash
set -euo pipefail

ION_SOURCE_DIR="${1:?Usage: apply-ion-patches.sh <ION source directory>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$SCRIPT_DIR/patches"

git -C "$ION_SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
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
