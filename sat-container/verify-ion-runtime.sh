#!/usr/bin/env bash
# Validate staged ELF executables without starting ION or creating an SDR.
set -euo pipefail
for binary in ionadmin ionsecadmin bpadmin ipnadmin bpsendfile bpsendtest \
  bprecvfile bpsource bpsink bpstats bpwatch rfxclock bpclock bptransit \
  bpclm ipnfw ipnadminep udpcli udpclo cpsd ionwarn killm; do
  executable="$(command -v "$binary")" || {
    echo "Missing ION executable: $binary" >&2
    exit 1
  }
  if ! dependencies="$(ldd "$executable" 2>&1)"; then
    printf 'Cannot inspect %s: %s\n' "$binary" "$dependencies" >&2
    exit 1
  fi
  if [[ "$dependencies" == *"not found"* ]]; then
    printf 'Unresolved dependencies for %s: %s\n' "$binary" "$dependencies" >&2
    exit 1
  fi
done
echo 'ION staged executable dependency checks passed (not a live startup test).'
