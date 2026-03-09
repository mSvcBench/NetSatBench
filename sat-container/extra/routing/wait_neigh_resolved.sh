#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <ip-address> [timeout-seconds] [interface] [poll-interval-seconds]" >&2
  echo "Example: $0 2001:db8::1 2.0 eth0 0.05" >&2
}

if [[ $# -lt 1 || $# -gt 4 ]]; then
  usage
  exit 2
fi

target_ip="$1"
timeout_s="${2:-1.0}"
dev="${3:-}"
interval_s="${4:-0.05}"

if [[ "$target_ip" == *:* ]]; then
  family="-6"
else
  family="-4"
fi

deadline_epoch="$(awk -v now="$(date +%s.%N)" -v t="$timeout_s" 'BEGIN{printf "%.6f", now + t}')"

while :; do
  if [[ -n "$dev" ]]; then
    line="$(ip "$family" neigh show to "$target_ip" dev "$dev" 2>/dev/null || true)"
  else
    line="$(ip "$family" neigh show to "$target_ip" 2>/dev/null || true)"
  fi

  if [[ -n "$line" ]]; then
    case " $line " in
      *" REACHABLE "*|*" STALE "*|*" DELAY "*|*" PROBE "*|*" PERMANENT "*|*" NOARP "*)
        exit 0
        ;;
    esac
  fi

  now_epoch="$(date +%s.%N)"
  timed_out="$(awk -v now="$now_epoch" -v deadline="$deadline_epoch" 'BEGIN{print (now >= deadline) ? "1" : "0"}')"
  if [[ "$timed_out" == "1" ]]; then
    exit 0
  fi

  sleep "$interval_s"
done
