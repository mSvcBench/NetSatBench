#!/usr/bin/env bash
set -euo pipefail

PREFIX="${ION_PREFIX:-/usr/local}"
OUT_DIR="${1:-ion-runtime}"

BIN_DIR="$OUT_DIR/bin"
LIB_DIR="$OUT_DIR/lib"

mkdir -p "$BIN_DIR" "$LIB_DIR"

required_bins=(
  ionadmin ionsecadmin bpadmin ipnadmin
  bpsendfile bprecvfile bpsource bpsink
  rfxclock bpclock bptransit bpclm
  ipnfw ipnadminep
  udpcli udpclo
  cpsd ionwarn killm
)

optional_bins=(
  bplist bpcancel bpstats bpstats2 bptrace bping
  ionlog ionunlock ionexit ionrestart ionstart ionstop ionrun
  ltpadmin ltpcli ltpclo ltpclock ltpdeliv ltpmeter
  udplsi udplso
)

missing=()
for bin in "${required_bins[@]}"; do
  if [ -x "$PREFIX/bin/$bin" ]; then
    cp -a "$PREFIX/bin/$bin" "$BIN_DIR/"
  else
    missing+=("$PREFIX/bin/$bin")
  fi
done

if [ "${#missing[@]}" -gt 0 ]; then
  printf 'Missing required ION binaries:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

for bin in "${optional_bins[@]}"; do
  [ -x "$PREFIX/bin/$bin" ] && cp -a "$PREFIX/bin/$bin" "$BIN_DIR/"
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

echo "ION runtime staged under: $OUT_DIR"
echo "Binaries: $(find "$BIN_DIR" -maxdepth 1 -type f | wc -l)"
echo "Libraries: $(find "$LIB_DIR" -maxdepth 1 \( -type f -o -type l \) | wc -l)"
