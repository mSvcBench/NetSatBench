# ION Node Registry

This directory contains the declarative ION identity data for the 10-node
example. The complete installation and experiment procedure is in the
[ION-DTN integration guide](../../../docs/ion-dtn-integration-guide.md).
The code-level changes are listed in [IMPLEMENTATION.md](IMPLEMENTATION.md).

`ion-node-registry.json` defines:

- the NetSatBench node configuration used for overlay address derivation;
- the default contact rate used only when an epoch link has no `rate`;
- per-link one-way light time (`owlt_seconds`);
- IPN node numbers; and
- the default source and destination application endpoints.

The registry deliberately does not contain UDP convergence-layer addresses or
a fixed relay path. The generator derives overlay addresses from
`../sat-config.json` and derives contacts, neighbors, rates, and candidate paths
from the selected epoch scenario.

| NetSatBench node | ION node | Application endpoint |
| --- | ---: | --- |
| `usr1` | 101 | `ipn:101.1` |
| `sat1` | 102 | `ipn:102.1` |
| `sat2` | 103 | `ipn:103.1` |
| `sat3` | 104 | `ipn:104.1` |
| `sat4` | 105 | `ipn:105.1` |
| `sat5` | 106 | `ipn:106.1` |
| `sat6` | 107 | `ipn:107.1` |
| `sat7` | 108 | `ipn:108.1` |
| `sat8` | 109 | `ipn:109.1` |
| `grd1` | 110 | `ipn:110.1` |

Generated configuration belongs in `ion/generated/` only when manual
inspection is needed. Normal runs use `nsb.py ion-prepare`, which generates
temporary files, copies each node's files into its container, and removes the
host-side temporary directory.
