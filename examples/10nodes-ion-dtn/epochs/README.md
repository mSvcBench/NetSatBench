# ION-DTN Epoch Demo Suite

This directory contains small, focused epoch scenarios for observing ION-DTN
behavior on the existing 10-node NetSatBench topology.

The baseline node set stays unchanged:

```text
usr1
sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8
grd1
```

The full 10-node chain used by the baseline scenarios is:

```text
usr1 -> sat1 -> sat2 -> sat3 -> sat4 -> sat5 -> sat6 -> sat7 -> sat8 -> grd1
```

## Scenarios

| Scenario | Purpose |
| --- | --- |
| `00-all-open` | Opens the full path at once for smoke testing ION and VXLAN routing. |
| `01-store-carry-forward` | Shows one bundle moving one hop per epoch with no end-to-end IP path. |
| `02-cloudy-ground` | Uses all eight satellites, then keeps the ground contact closed so the bundle waits on `sat8`. |
| `03-alternate-path` | Tests clockwise/counterclockwise CGR selection on one eight-satellite orbital ring. |
| `04-multi-bundle-queue` | Tests bulk, standard, and urgent queue service across two capacity-limited `sat4-sat5` contacts and a recovery contact. |
| `05-short-contact-capacity` | Changes the middle link from 10 Mbit/s through 1 Mbit/s and 100 Kbit/s during one contact. |
| `06-handover-overlap` | Replays the native 61-epoch handover clock with make-before-break access links and guarded ION contacts. |

## Running A Scenario

Use `ion-prepare` and `run` with one shared future `T0`. The complete command
sequence, including deployment, receiver setup, and assertions, is in the
[integration guide](../../../docs/ion-dtn-integration-guide.md).

```bash
.venv/bin/python ./nsb.py ion-prepare --list-scenarios
```

## Important Notes

- Every scenario references the full registry of 10 nodes.
- `00`, `01`, `02`, and `04` are baseline pass/fail demonstrations.
- `03-alternate-path` uses only adjacent satellites in `sat_orbit: 1`. Its
  preferred clockwise path is swept from 10 Mbit/s below the fixed 5 Mbit/s
  counterclockwise path, removed, and restored through the same rates.
- `05-short-contact-capacity` is a dynamic-rate boundary/stress scenario.
  Its `sat4-sat5` rate changes are reflected in both NetSatBench shaping and
  the generated ION contact-plan volume.
- `06-handover-overlap` removes the native IP routing oracle while preserving
  its link-add and traffic-injection cadence while applying the selected
  6-second overlap treatment. A 4-second start guard and 1-second end guard
  leave 1 second of ION overlap.
- Apply epochs one at a time only for topology inspection. Timed CGR tests must
  replay the complete scenario with the same absolute start time used by ION.
