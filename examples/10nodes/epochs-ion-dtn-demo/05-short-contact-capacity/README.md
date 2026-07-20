# 05 - Short Contact Capacity

Purpose: stress short contacts and show where payload size starts to matter.
This is not a simple "everything must pass" scenario. It has two useful modes:

```text
small payload: expected to pass
large payload: expected to expose contact-capacity limits and partial acquisition
```

Epoch layout:

```text
epoch0: usr1 <-> sat1
epoch1: sat1 <-> sat2
epoch2: sat2 <-> sat3
epoch3: sat3 <-> sat4
epoch4: sat4 <-> grd1
```

Recommended contact generation:

```bash
.venv/bin/python ./nsb.py ion-generate-configs \
  --spec examples/10nodes/ion-dtn/path-spec.json \
  --output examples/10nodes/ion-dtn/generated \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/05-short-contact-capacity \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 15
```

Recommended experiment:

```text
small payload: should pass through the chain
large payload: may fail to complete before the final contact closes
```

Observed failure shape for oversized payloads:

```text
usr1/sat relays may show xmt/rcv counters
grd1 may contain bpacq.* partial acquisition files
testfileN is created only when the whole bundle is received
```

Use this scenario to measure the boundary between "fits in contact window" and
"partially arrives but does not complete". To make it stricter, reduce
`contact_rate_bytes_per_second` in `path-spec.json`; to make it easier, increase
`--epoch-duration` or keep the final `sat4 <-> grd1` contact open longer.
