# ION-DTN Epoch Demo Suite

This directory contains small, focused epoch scenarios for observing ION-DTN
behavior on the existing 10-node NetSatBench topology.

The baseline node set stays unchanged:

```text
usr1
sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8
grd1
```

The main ION path used by the current generator is:

```text
usr1 -> sat1 -> sat2 -> sat3 -> sat4 -> grd1
```

## Scenarios

| Scenario | Purpose |
| --- | --- |
| `00-all-open` | Opens the full path at once for smoke testing ION and VXLAN routing. |
| `01-store-carry-forward` | Shows one bundle moving one hop per epoch with no end-to-end IP path. |
| `02-cloudy-ground` | Keeps the ground contact closed for two epochs so the bundle waits on `sat4`. |
| `03-alternate-path` | Opens a possible secondary physical branch through `sat5` and `sat6`. |
| `04-multi-bundle-queue` | Lets you send multiple bundles at different times and watch queueing. |
| `05-short-contact-capacity` | Creates intentionally short contacts for payload/rate stress tests. |

## Common Contact Plan Generation

Run from the NetSatBench repository root:

```bash
.venv/bin/python ./nsb.py ion-generate-configs \
  --spec examples/10nodes/ion-dtn/path-spec.json \
  --output examples/10nodes/ion-dtn/generated \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/01-store-carry-forward \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 45
```

Change only `--epoch-dir` to switch scenarios.

## Important Notes

- `00`, `01`, `02`, and `04` are baseline pass/fail demonstrations.
- `03-alternate-path` derives ION contacts and neighbor plans from the scenario
  links, so it can exercise a branch through `sat5` and `sat6`.
- `05-short-contact-capacity` is a boundary/stress scenario. Small payloads
  should pass; oversized payloads may leave `bpacq.*` partial acquisition files
  instead of complete `testfileN` deliveries.
- For DTN observation, send the bundle early, then apply epochs one by one.
  The interesting behavior is the bundle waiting at relay nodes until the next
  planned contact opens.
