# ION All-Open Epoch Scenario

This scenario keeps the complete ION test path open at the same time:

```text
usr1 <-> sat1 <-> sat2 <-> sat3 <-> sat4 <-> grd1
```

It is useful for isolating ION-DTN forwarding from timed contact/epoch behavior.
If bundle forwarding fails here, debug VXLAN, Linux routes, FRR/ISIS, or ION
configuration before testing store-carry-forward epochs.

Run it from the NetSatBench repository root:

```bash
HOST_IP="<host-ip-reachable-from-workers>"
ETCD_HOST="$HOST_IP"

python3 ./nsb.py run \
  --epoch-dir examples/10nodes/epochs-ion-dtn-demo/00-all-open \
  --file-pattern 'NetSatBench-epoch0.json' \
  --fixed-wait 3 \
  --etcd-host "$ETCD_HOST"
```

Expected active links:

```text
usr1-sat1
sat1-sat2
sat2-sat3
sat3-sat4
sat4-grd1
```
