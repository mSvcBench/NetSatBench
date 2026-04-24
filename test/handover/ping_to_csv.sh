#!/bin/bash

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 <target|default> <duration_seconds> [interval_seconds]" >&2
    exit 1
fi

TARGET="$1"
if [ "$TARGET" = "default" ]; then
    read -r _ TARGET < "./grd_config" || {
        echo "Failed to read target from ./grd_config" >&2
        exit 1
    }
fi
DURATION="$2"
INTERVAL="${3:-0.01}"
SIZE="1200"
OUTPUT="ping_${TARGET}.csv"
RAW_OUTPUT="ping_${TARGET}.raw"
COUNT="$(awk -v duration="$DURATION" -v interval="$INTERVAL" 'BEGIN { print int(duration / interval) }')"

# Write CSV header
echo "timestamp,icmp_seq,ttl,rtt_ms" > "$OUTPUT"

# Run ping and store raw output first
ping -i "$INTERVAL" "$TARGET" -c "$COUNT" -s "$SIZE" > "$RAW_OUTPUT"

# Parse stored ping output after ping completes
awk -v outfile="$OUTPUT" '
/bytes from/ {
    # Extract RTT
    split($0,a,"time=");
    split(a[2],b," ");
    rtt=b[1];

    # Extract sequence number
    split($6,seq,"=");
    icmp_seq=seq[2];

    # Extract TTL
    split($7,t,"=");
    ttl=t[2];

    # Timestamp in ISO format
    cmd="date +%Y-%m-%dT%H:%M:%S.%3N";
    cmd | getline timestamp;
    close(cmd);

    print timestamp "," icmp_seq "," ttl "," rtt >> outfile;
    fflush(outfile);
}
' "$RAW_OUTPUT"
