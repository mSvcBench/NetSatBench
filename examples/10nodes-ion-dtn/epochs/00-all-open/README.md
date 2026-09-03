# ION All-Open Epoch Scenario

This scenario keeps the complete 10-node ION test path open at the same time:

```text
usr1 <-> sat1 <-> sat2 <-> sat3 <-> sat4 <-> sat5
     <-> sat6 <-> sat7 <-> sat8 <-> grd1
```

It is useful for isolating ION-DTN forwarding from timed contact/epoch behavior.
If bundle forwarding fails here, debug VXLAN, direct-neighbor routes, or ION
configuration before testing store-carry-forward epochs. Multi-hop IP routing
is intentionally disabled; CGR forwards the bundle hop by hop.

The subsecond development variant uses a deterministic `5ms` one-way delay on
every hop.  The epoch timestamp includes `.125Z` to exercise millisecond input
parsing, and the generated ION plan must retain every range as `0.005s` under
the `ceil-millisecond` policy.  This is a protocol baseline, not a claim that
all real satellite links have the same propagation delay.

The pilot installs a `4.000s` start guard and a `1.500s` end guard.  It rejects
containers that do not use `netsatbench-sat-container-ion:subsecond` and saves
the installed `global.ionrc` as evidence before sending a bundle.

Run it from the NetSatBench repository root:

```bash
HOST_IP="<host-ip-reachable-from-workers>"
ETCD_HOST="$HOST_IP"

python3 ./nsb.py run \
  --epoch-dir examples/10nodes-ion-dtn/epochs/00-all-open \
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
sat4-sat5
sat5-sat6
sat6-sat7
sat7-sat8
sat8-grd1
```
