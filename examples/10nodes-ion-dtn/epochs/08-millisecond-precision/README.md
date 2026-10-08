# 08 - Millisecond Precision

Purpose: keep the proven second-scale timing and change exactly one thing --
whether an epoch boundary that does not fall on a whole second survives
generation, storage and dispatch.

Contact lengths, link rates and the canonical 4 s / 1 s guards stay in the
range the earlier campaigns already validated. Only the epoch offsets carry a
fractional part, and the one-way light time is expressed in milliseconds
instead of being rounded up to a whole second:

```text
epoch0 at +0        complete usr1 -> sat1 -> sat2 -> grd1 path opens at 10 Mbit/s
epoch1 at +10.237s  sat1-sat2 drops to 1 Mbit/s
epoch2 at +20.481s  sat1-sat2 drops to 100 Kbit/s
epoch3 at +30.719s  sat1-sat2 recovers to 10 Mbit/s
epoch4 at +40.953s  all links close
```

The offsets mirror a measured pass whose usable window ran from 08:40:54.803
to 08:41:15.773; the link delay is 6 ms rather than a rounded second.

## Millisecond dispatch is opt-in

ION keeps its legacy 1000 ms `rfxclock` poll interval unless
`ION_RFXCLOCK_POLL_MSEC` is set. Without it the fractional boundaries are still
parsed and stored exactly, but they are only acted upon once per second, which
defeats the purpose of this scenario. Enable it per node in the satellite
configuration before deploying:

```json
"env": { "ION_RFXCLOCK_POLL_MSEC": "10" }
```

The value is passed through to `docker run` by `nsb-deploy`. It is deliberately
absent from the checked-in configuration so that the default testbed behaviour
stays identical to the whole-second baseline.

## Recommended contact generation

```bash
.venv/bin/python ./nsb.py ion-prepare \
  --scenario 08-millisecond-precision \
  --epoch-duration 10.237 \
  --contact-start-guard 4 \
  --contact-end-guard 1 \
  --absolute-start-time <shared T0>
```

Drive the physical epochs from the same `T0` so that the emulated data plane
and the ION contact plan share one time base:

```bash
.venv/bin/python ./nsb.py run \
  --epoch-dir examples/10nodes-ion-dtn/epochs/08-millisecond-precision \
  --file-pattern 'NetSatBench-epoch*.json' \
  --absolute-start-time <shared T0>
```

## Guarded bottleneck volume

Derived from the four rate segments on `sat1 <-> sat2`:

```text
 6.237 s x 10 Mbit/s  =  7,796,250 B
10.244 s x  1 Mbit/s  =  1,280,500 B
10.238 s x  0.1 Mbit/s =   127,975 B
 9.234 s x 10 Mbit/s  = 11,542,500 B
total                 = 20,747,225 B
```

## Result contract

```text
10 KiB positive control: delivered, SHA-256 matches the source
24 MiB negative control: must not complete before the contact closes
```

A payload injected at `T0 + 5 s` has about 19.5 MB of guarded volume left, so
24 MiB exceeds the bottleneck by roughly 1.29x. Both halves matter: the
positive control alone cannot distinguish a working capacity model from one
that forwards everything regardless of the plan.
