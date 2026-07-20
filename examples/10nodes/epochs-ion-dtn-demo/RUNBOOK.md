# ION-DTN Demo Runbook

Run commands from the NetSatBench repository root:

```bash
cd /path/to/NetSatBench
HOST_IP="<host-ip-reachable-from-workers>"
ETCD_HOST="$HOST_IP"
NODE_ETCD_HOST="$HOST_IP"
```

`NODE_ETCD_HOST` must be reachable from inside every node container. For
multi-worker runs, use the Etcd server's private/routable IP rather than
`127.0.0.1`.

If the ION image is local, load it onto all workers before deployment:

```bash
.venv/bin/python ./nsb.py load-image \
  --image netsatbench-sat-container-ion:prototype \
  --worker-config examples/10nodes/worker-config.json \
  -t 4
```

## 1. Generate ION Configs For A Scenario

Example for the cloudy ground station test:

```bash
.venv/bin/python ./nsb.py ion-generate-configs \
  --spec examples/10nodes/ion-dtn/path-spec.json \
  --output examples/10nodes/ion-dtn/generated \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 45
```

Use these scenario directories:

```text
examples/10nodes/epochs-ion-dtn-demo/01-store-carry-forward
examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground
examples/10nodes/epochs-ion-dtn-demo/03-alternate-path
examples/10nodes/epochs-ion-dtn-demo/04-multi-bundle-queue
examples/10nodes/epochs-ion-dtn-demo/05-short-contact-capacity
```

For `05-short-contact-capacity`, use `--epoch-duration 15`.

## 2. Apply Epochs Manually

Apply one epoch at a time so you can inspect the network between contacts:

```bash
.venv/bin/python ./nsb.py run \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch0.json' \
  --fixed-wait 3 \
  --etcd-host "$ETCD_HOST"
```

Then change `epoch0` to `epoch1`, `epoch2`, and so on.

## 3. Observe Physical Links

Check which VXLAN links are active:

```bash
docker exec etcd etcdctl --endpoints=127.0.0.1:2379 \
  get /config/links --prefix --keys-only | sort
```

Check a node route decision:

```bash
docker exec sat1 ip route get 172.100.0.6
docker exec sat4 ip route get 172.101.0.2
```

Watch packets on a link:

```bash
docker exec sat1 tcpdump -ni vl_sat2_1 udp port 1113
docker exec sat2 tcpdump -ni vl_sat1_1 udp port 1113
```

## 4. Observe ION State

Useful commands inside a container:

```bash
docker exec sat2 sh -lc 'tail -n 80 /ion/sat2/ion.log 2>/dev/null || true'
docker exec sat2 sh -lc 'ipnadmin node.ipnrc'
docker exec sat2 sh -lc 'bpadmin node.bprc'
```

Useful log patterns:

```text
rcv  - bundle received by this node
fwd  - bundle forwarded by BP/IPN logic
xmt  - bundle transmitted by convergence layer
```

## 5. Send A Bundle

Start the receiver on `grd1`:

```bash
docker exec grd1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/grd1
  nohup bprecvfile ipn:110.1 1 > receiver.log 2>&1 &
'
```

Send from `usr1`:

```bash
docker exec usr1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/usr1
  printf "DTN demo payload $(date +%H%M%S)\n" > payload.txt
  bpsendfile ipn:101.1 ipn:110.1 payload.txt 0.1.0 600
'
```

Check delivery:

```bash
docker exec grd1 sh -lc '
  cd /ion/grd1
  ls -la testfile* receiver.log 2>/dev/null || true
  cat testfile1 2>/dev/null || true
'
```

## 6. Scenario-Specific Observations

### 01 - Store-Carry-Forward

Send the bundle during `epoch0`.

Expected observation:

```text
The bundle moves one hop per epoch and reaches grd1 at epoch4.
```

### 02 - Cloudy Ground Station

Send the bundle during `epoch0`.

Expected observation:

```text
The bundle reaches sat4 by epoch3.
It waits during epoch4 and epoch5 because grd1 contact is missing.
It reaches grd1 at epoch6.
```

### 03 - Alternate Physical Path

Use this to check CGR behavior when the physical contact graph contains a
branch through `sat5` and `sat6`.

Expected observation:

```text
sat1 sees sat2 and sat5 as physical neighbors.
ION has direct-neighbor plans for the contacts present in this scenario.
The bundle should reach grd1 at epoch4 through the available contact graph.
```

### 04 - Multi-Bundle Queue

Send Bundle A in `epoch0`, Bundle B in `epoch1`, and optionally Bundle C before
`epoch3`.

Expected observation:

```text
Bundles are stored and released across multiple epochs.
Delivery times differ because each bundle enters the chain at a different time.
```

### 05 - Short Contact Capacity

Generate with `--epoch-duration 15`.

Expected observation:

```text
Small payloads should pass.
Large payloads make contact capacity visible.
```

To make capacity pressure more obvious, reduce
`contact_rate_bytes_per_second` in `path-spec.json`, regenerate configs, and
send a larger file.
