#!/bin/bash

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "Usage: $0 <target|default> <port> <duration_seconds> [interval_seconds]" >&2
    exit 1
fi

TARGET="$1"
if [ "$TARGET" = "default" ]; then
    read -r _ TARGET < "./grd_config" || {
        echo "Failed to read target from ./grd_config" >&2
        exit 1
    }
fi

DURATION="$3"
INTERVAL="${4:-1}"
OUTPUT="iperf3_${TARGET}.csv"
RAW_OUTPUT="iperf3_${TARGET}.raw"

echo "timestamp,interval_start_s,interval_end_s,bytes,bits_per_second,retransmits" > "$OUTPUT"

iperf3 -c "$TARGET" -p "$2" -t "$DURATION" -w 256k -i "$INTERVAL" -J > "$RAW_OUTPUT"
# iperf3 -c "$TARGET" -p "$2" -t "$DURATION" -i "$INTERVAL" -J > "$RAW_OUTPUT"

jq -r '
    .start.timestamp.timesecs as $base
    | .intervals[]
    | .streams[0] as $stream
    | [
        (($base + $stream.start) | strftime("%Y-%m-%dT%H:%M:%S")),
        $stream.start,
        $stream.end,
        $stream.bytes,
        $stream.bits_per_second,
        ($stream.retransmits // "")
      ]
    | @csv
' "$RAW_OUTPUT" >> "$OUTPUT"
