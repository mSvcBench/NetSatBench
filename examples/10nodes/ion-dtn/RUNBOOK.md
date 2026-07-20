# ION-DTN Integration Runbook

Run from the NetSatBench repository root:

```bash
cd /path/to/NetSatBench
HOST_IP="<host-ip-reachable-from-workers>"
ETCD_HOST="$HOST_IP"
NODE_ETCD_HOST="$HOST_IP"
```

`ETCD_HOST` is the address used by the control host. `NODE_ETCD_HOST` is the
address used from inside node containers. In multi-worker runs, set
`NODE_ETCD_HOST` to an Etcd IP reachable from every worker/container.

## 1. Prepare ION Automatically

This is the preferred path. It generates ION configs in a temporary directory,
copies only the required files into each selected container, starts ION, and
leaves no generated config tree in the repository:

```bash
.venv/bin/python ./nsb.py ion-prepare \
  --scenario 02-cloudy-ground \
  --epoch-duration 45 \
  --jobs 8
```

List available scenarios:

```bash
.venv/bin/python ./nsb.py ion-prepare --list-scenarios
```

For the short contact scenario:

```bash
.venv/bin/python ./nsb.py ion-prepare \
  --scenario 05-short-contact-capacity \
  --epoch-duration 15 \
  --jobs 8
```

Use `--no-start` if you only want to copy configs without starting ION.

For CGR timing tests, prefer one shared absolute T0:

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
  --etcd-host "$ETCD_HOST"
```

Do not mix `--fixed-wait` with CGR contact-plan tests. `--fixed-wait` is useful
for quick topology checks, but CGR needs NetSatBench epochs and ION contacts to
share the same time base.

## 2. Generate ION Configs Manually

Cloudy ground station demo:

```bash
.venv/bin/python ./nsb.py ion-generate-configs \
  --spec examples/10nodes/ion-dtn/path-spec.json \
  --output examples/10nodes/ion-dtn/generated \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 45
```

For the short contact scenario, use `--epoch-duration 15`.

## 3. Deploy Or Restart NetSatBench

Use the ION-enabled image config:

If the image is local and `image-pull-policy` is `never`, load it onto all
workers first:

```bash
.venv/bin/python ./nsb.py load-image \
  --image netsatbench-sat-container-ion:prototype \
  --worker-config examples/10nodes/worker-config.json \
  -t 4
```

```bash
.venv/bin/python ./nsb.py init \
  --config ./examples/10nodes/sat-config.ion.json \
  --etcd-host "$ETCD_HOST"

.venv/bin/python ./nsb.py deploy -t 8 \
  --etcd-host "$ETCD_HOST" \
  --node-etcd-host "$NODE_ETCD_HOST"
```

## 4. Copy ION Configs Into Containers Manually

```bash
for n in usr1 sat1 sat2 sat3 sat4 grd1; do
  docker exec "$n" sh -lc 'mkdir -p /ion'
  docker cp examples/10nodes/ion-dtn/generated/. "$n":/ion/
done
```

Each container should have:

```text
/ion/global.ionrc
/ion/<node>/node.ionrc
/ion/<node>/node.bprc
/ion/<node>/node.ipnrc
```

## 5. Start ION Manually

```bash
for n in usr1 sat1 sat2 sat3 sat4 grd1; do
  docker exec "$n" sh -lc "
    export ION_NODE_LIST_DIR=/ion
    cd /ion/$n
    ionadmin node.ionrc > ion.log 2>&1
    ionsecadmin node.ionsecrc >> ion.log 2>&1
    bpadmin node.bprc >> ion.log 2>&1
  "
done
```

## 6. Apply Epochs Manually

Manual epoch replay is useful for debugging topology only. For ION/CGR bundle
tests, use the absolute-time command in step 1.

Example for epoch0:

```bash
.venv/bin/python ./nsb.py run \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch0.json' \
  --fixed-wait 3 \
  --etcd-host "$ETCD_HOST"
```

Then repeat with `epoch1`, `epoch2`, and so on.

## 7. Send And Receive A Bundle

Receiver:

```bash
docker exec grd1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/grd1
  nohup bprecvfile ipn:110.1 1 > receiver.log 2>&1 &
'
```

Sender:

```bash
docker exec usr1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/usr1
  printf "DTN demo payload $(date +%H%M%S)\n" > payload.txt
  bpsendfile ipn:101.1 ipn:110.1 payload.txt 0.1.0 600
'
```

Delivery check:

```bash
docker exec grd1 sh -lc '
  cd /ion/grd1
  ls -la testfile* receiver.log 2>/dev/null || true
  cat testfile1 2>/dev/null || true
'
```

## 8. Observe

Physical links:

```bash
docker exec etcd etcdctl --endpoints=127.0.0.1:2379 \
  get /config/links --prefix --keys-only | sort
```

Routes:

```bash
docker exec sat1 ip route get 172.100.0.6
docker exec sat4 ip route get 172.101.0.2
```

ION logs:

```bash
for n in usr1 sat1 sat2 sat3 sat4 grd1; do
  echo "=== $n ==="
  docker exec "$n" sh -lc "tail -n 60 /ion/$n/ion.log 2>/dev/null || true"
done
```

Log meaning:

```text
rcv: bundle received by this node
fwd: bundle selected for forwarding
xmt: bundle transmitted by the convergence layer
```

Short-contact capacity notes:

```text
testfileN   complete bundle delivered to the app
bpacq.*     partial UDP CLA acquisition, usually contact closed before completion
```

If `05-short-contact-capacity` only delivers the small payload, that is a valid
capacity observation, not a baseline routing failure.
