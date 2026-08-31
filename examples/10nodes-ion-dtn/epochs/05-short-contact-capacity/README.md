# 05 - Short Contact Capacity

Purpose: stress short contacts and show where payload size starts to matter.
This is not a simple "everything must pass" scenario. It has two useful modes:

```text
small payload: expected to pass
payload larger than the guarded bottleneck volume: expected not to complete
```

All nine links in the 10-node chain open together. The canonical 4 s start and 1 s end guards remain active. The middle
`sat4 <-> sat5` link changes capacity while the contact remains active:

```text
epoch0 at +0s:  complete chain opens at 10 Mbit/s
epoch1 at +10s:  sat4-sat5 drops to 1 Mbit/s
epoch2 at +20s: sat4-sat5 drops to 100 Kbit/s
epoch3 at +30s: sat4-sat5 recovers to 10 Mbit/s
epoch4 at +40s: all links close
```

Recommended contact generation:

```bash
.venv/bin/python ./nsb.py ion-generate-configs \
  --registry examples/10nodes-ion-dtn/ion/ion-node-registry.json \
  --output examples/10nodes-ion-dtn/ion/generated \
  --epoch-dir examples/10nodes-ion-dtn/epochs/05-short-contact-capacity \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 10
```

The guarded bottleneck volume is computed from the four rate segments:

```text
 6 s x 10 Mbit/s  =  7,500,000 B
10 s x  1 Mbit/s  =  1,250,000 B
10 s x  0.1 Mbit/s =   125,000 B
 9 s x 10 Mbit/s  = 11,250,000 B
total             = 20,125,000 B
```

Recommended experiment:

```text
10 KiB positive control: must pass through the chain with matching SHA-256
24 MiB capacity negative control: must not complete before the contact closes
```

Run the guarded, result-contract-aware pilot with:

```bash
python labs/netsatbench/ion/analysis/run_short_contact_capacity.py \
  --etcd-host "$ETCD_HOST"
```

Observed failure shape for oversized payloads:

```text
usr1/sat relays may show xmt/rcv counters
grd1 may contain bpacq.* partial acquisition files
testfileN is created only when the whole bundle is received
```

Use this scenario to measure whether CGR's planned volume follows the same rate
timeline as NetSatBench traffic shaping. Change the epoch `rate` values to make
the bottleneck stricter or easier; `default_contact_rate_bytes_per_second` is now only a
fallback when a link has no epoch rate.
