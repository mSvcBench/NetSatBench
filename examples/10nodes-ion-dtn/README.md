# 10-Node ION-DTN Example

This self-contained example runs ION-DTN over a ten-node NetSatBench topology:

```text
usr1 -> sat1 -> sat2 -> sat3 -> sat4 -> sat5 -> sat6 -> sat7 -> sat8 -> grd1
```

The satellite metadata defines one orbital plane containing the eight
satellites in position order. Baseline scenarios open a linear subset of those
links; `03-alternate-path` adds `sat8 <-> sat1` to complete the ring and tests
both directions between `sat1` and `sat5`.

## Contents

- `sat-config.json`: NetSatBench topology using the ION-enabled node image.
- `worker-config.example.json`: non-secret worker configuration template.
- `epochs/`: seven contact, capacity, and handover scenarios.
- `ion/`: IPN node registry, operational runbook, and implementation notes.

Start with the complete
[installation and experiment guide](../../docs/ion-dtn-integration-guide.md).
See [`ion/IMPLEMENTATION.md`](ion/IMPLEMENTATION.md) for a file-by-file summary
of the NetSatBench code changes.
The README in each scenario directory records its timeline and expected result.

The deterministic demo epochs use a `1s` NetEm one-way delay, matching the
registry's whole-second `owlt_seconds: 1` ION range value. This keeps the
emulated link and ION planning models consistent; it is not intended to model
a physical LEO propagation delay.

When an epoch link supplies `delay`, the ION generator derives that contact
segment's OWLT from the epoch instead of the registry fallback. The bundled
ION version accepts only positive whole-second OWLT values, so delays are
rounded up (`4.7ms` becomes `1s`, `1.2s` becomes `2s`).

Regenerate the checked-in scenarios after changing their declarative source:

```bash
python3 nsb.py ion-generate-demo-scenarios
python3 nsb.py ion-generate-demo-scenarios --check
```

The original `examples/10nodes` example remains independent and continues to
provide the standard IPv4, IPv6, and oracle-routing configurations.
