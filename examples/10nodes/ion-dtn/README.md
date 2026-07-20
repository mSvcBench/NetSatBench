# NetSatBench ION-DTN Integration

This directory keeps the reusable ION-DTN integration assets for NetSatBench.

Default DTN application flow:

```text
usr1 -> grd1
```

The relay path is not hard-coded in `path-spec.json`. It is derived from the
selected NetSatBench epoch scenario and the resulting ION contact graph.

NetSatBench provides the changing IP/VXLAN topology. ION runs above that
topology and forwards bundles with planned contacts:

```text
NetSatBench epochs
        |
        v
VXLAN links and direct neighbor routes
        |
        v
ION UDP convergence layer
        |
        v
Bundle Protocol / IPN forwarding
```

## Contents

```text
examples/10nodes/ion-dtn/
|-- README.md
|-- RUNBOOK.md
|-- WHAT_WE_DID.md
`-- path-spec.json
```

The executable entry points live in the normal NetSatBench CLI:

```text
control/nsb-ion-prepare.py
utils/nsb-ion-generate-configs.py
```

Generated ION configs are intentionally not stored here as source truth. Use the
scenario helper to generate configs in a temporary host directory, copy only the
needed files into each container, start ION, and remove the temporary host files:

```bash
cd /path/to/NetSatBench

.venv/bin/python ./nsb.py ion-prepare \
  --scenario 02-cloudy-ground \
  --epoch-duration 45 \
  --jobs 8
```

For CGR tests, give ION and NetSatBench the same absolute T0:

```bash
T0="$(date -u -d '+3 minutes' '+%Y-%m-%dT%H:%M:%SZ')"

.venv/bin/python ./nsb.py ion-prepare \
  --scenario 02-cloudy-ground \
  --epoch-duration 45 \
  --absolute-start-time "$T0" \
  --jobs 8

.venv/bin/python ./nsb.py run \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch*.json' \
  --absolute-start-time "$T0" \
  --etcd-host 127.0.0.1
```

## Multi-Worker Notes

The ION-enabled image is configured as a local image:

```json
"image": "netsatbench-sat-container-ion:prototype",
"image-pull-policy": "never"
```

For multi-worker runs, load that image onto every worker before `deploy`:

```bash
.venv/bin/python ./nsb.py load-image \
  --image netsatbench-sat-container-ion:prototype \
  --worker-config examples/10nodes/worker-config.json \
  -t 4
```

When deploying, pass an Etcd address reachable from inside every container:

```bash
export ETCD_HOST="<control-reachable-etcd-ip>"
export NODE_ETCD_HOST="<container-reachable-etcd-ip>"

.venv/bin/python ./nsb.py deploy -t 8 \
  --etcd-host "$ETCD_HOST" \
  --node-etcd-host "$NODE_ETCD_HOST"
```

Do not use `127.0.0.1` as `NODE_ETCD_HOST` for deployed containers. In a
single-host Docker bridge setup it may be `172.20.0.1`; in multi-worker setups
it should normally be the Etcd server's private/routable IP.

## Node Registry

| NetSatBench node | Overlay IP | ION node | App endpoint |
| --- | --- | ---: | --- |
| `usr1` | `172.102.0.2` | `101` | `ipn:101.1` |
| `sat1` | `172.100.0.2` | `102` | `ipn:102.1` |
| `sat2` | `172.100.0.6` | `103` | `ipn:103.1` |
| `sat3` | `172.100.0.10` | `104` | `ipn:104.1` |
| `sat4` | `172.100.0.14` | `105` | `ipn:105.1` |
| `sat5` | `172.100.0.18` | `106` | `ipn:106.1` |
| `sat6` | `172.100.0.22` | `107` | `ipn:107.1` |
| `sat7` | `172.100.0.26` | `108` | `ipn:108.1` |
| `sat8` | `172.100.0.30` | `109` | `ipn:109.1` |
| `grd1` | `172.101.0.2` | `110` | `ipn:110.1` |

`path-spec.json` is a node registry plus default source/destination. Scenario
epoch files decide which links become ION contacts.

## Epoch Demo Suite

The organized DTN scenarios live here:

```text
examples/10nodes/epochs-ion-dtn-demo/
```

Use:

```text
00-all-open              smoke test, all links open
01-store-carry-forward   one hop per epoch
02-cloudy-ground         final ground contact is delayed
03-alternate-path        physical alternate branch exploration
04-multi-bundle-queue    multiple bundles over intermittent contacts
05-short-contact-capacity short contact / capacity-boundary stress
```

The most useful first DTN demonstration is `02-cloudy-ground`: the bundle
reaches `sat4`, waits while the ground station contact is unavailable, then is
delivered when `sat4 <-> grd1` opens.

`05-short-contact-capacity` is intentionally a boundary test. Small payloads
should pass; oversized payloads may leave `bpacq.*` partial-acquisition files on
`grd1` instead of producing a complete `testfileN`.
