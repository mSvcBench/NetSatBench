#!/usr/bin/env bash
set -euo pipefail

PREFIX="${ION_PREFIX:-/usr/local}"
OUT_DIR="${1:-ion-runtime}"

OUT_PATH="$(readlink -m -- "$OUT_DIR")"
PREFIX_PATH="$(readlink -m -- "$PREFIX")"
CURRENT_PATH="$(pwd -P)"
if [[ "$OUT_PATH" == "/" || "$OUT_PATH" == "$CURRENT_PATH" || "$OUT_PATH" == "$PREFIX_PATH" ]]; then
  printf 'Refusing unsafe ION runtime output path: %s\n' "$OUT_PATH" >&2
  exit 2
fi

OUT_PARENT="$(dirname -- "$OUT_PATH")"
OUT_NAME="$(basename -- "$OUT_PATH")"
mkdir -p -- "$OUT_PARENT"

STAGE_DIR="$(mktemp -d "$OUT_PARENT/.${OUT_NAME}.stage.XXXXXX")"
BACKUP_DIR=""
cleanup() {
  if [[ -n "$STAGE_DIR" && -e "$STAGE_DIR" ]]; then
    rm -rf -- "$STAGE_DIR"
  fi
  if [[ -n "$BACKUP_DIR" && -e "$BACKUP_DIR" ]]; then
    rm -rf -- "$BACKUP_DIR"
  fi
}
trap cleanup EXIT

BIN_DIR="$STAGE_DIR/bin"
LIB_DIR="$STAGE_DIR/lib"
mkdir -p -- "$BIN_DIR" "$LIB_DIR"

required_bins=(
  ionadmin ionsecadmin bpadmin ipnadmin
  bpsendfile bpsendtest bprecvfile bpsource bpsink
  bpstats bpwatch
  rfxclock bpclock bptransit bpclm
  ipnfw ipnadminep
  udpcli udpclo
  cpsd ionwarn killm
)

optional_bins=(
  bplist bpcancel bpstats2 bptrace bping
  ionlog ionunlock ionexit ionrestart ionstart ionstop ionrun
  ltpadmin ltpcli ltpclo ltpclock ltpdeliv ltpmeter
  udplsi udplso
)

missing=()
for bin in "${required_bins[@]}"; do
  if [[ -x "$PREFIX/bin/$bin" ]]; then
    cp -a -- "$PREFIX/bin/$bin" "$BIN_DIR/"
  else
    missing+=("$PREFIX/bin/$bin")
  fi
done

if [[ "${#missing[@]}" -gt 0 ]]; then
  printf 'Missing required ION binaries:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

for bin in "${optional_bins[@]}"; do
  [[ -x "$PREFIX/bin/$bin" ]] && cp -a -- "$PREFIX/bin/$bin" "$BIN_DIR/"
done

ion_libs=(
  libamp libampAgentADM libams
  libbp libbss libbssp libcfdp libcgr libdgr
  libdtka libdtpc libeppcla libgfcomplete
  libici libionfec libjerasure libltp
  libmetadata libsecrypt libsppcla libstcpcla
  libtc libtcaP libtcc libtcpbsa
  libudpcla libudplsa libxlsa
)

for lib in "${ion_libs[@]}"; do
  compgen -G "$PREFIX/lib/${lib}.so*" >/dev/null && cp -a "$PREFIX/lib/${lib}.so"* "$LIB_DIR/"
done

if [[ -e "$OUT_PATH" || -L "$OUT_PATH" ]]; then
  BACKUP_DIR="$OUT_PARENT/.${OUT_NAME}.old.$$"
  mv -- "$OUT_PATH" "$BACKUP_DIR"
fi

if ! mv -- "$STAGE_DIR" "$OUT_PATH"; then
  if [[ -n "$BACKUP_DIR" && -e "$BACKUP_DIR" ]]; then
    mv -- "$BACKUP_DIR" "$OUT_PATH"
    BACKUP_DIR=""
  fi
  exit 1
fi
STAGE_DIR=""

if [[ -n "$BACKUP_DIR" ]]; then
  rm -rf -- "$BACKUP_DIR"
  BACKUP_DIR=""
fi

printf 'ION runtime staged under: %s\n' "$OUT_PATH"
printf 'Binaries: %s\n' "$(find "$OUT_PATH/bin" -maxdepth 1 -type f | wc -l)"
printf 'Libraries: %s\n' "$(find "$OUT_PATH/lib" -maxdepth 1 \( -type f -o -type l \) | wc -l)"
