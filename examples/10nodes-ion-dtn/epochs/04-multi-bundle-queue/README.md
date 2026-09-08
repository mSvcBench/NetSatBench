# 04 - Multi-Bundle Priority Queue

This scenario tests priority-aware ION queue service at the `sat4-sat5`
bottleneck. Every other hop advertises 2 Mbit/s of BP service to ION. All
links use 100 ms one-way delay. The closing bottleneck uses a 10 Mbit/s NetEm
wire rate so its one-second end guard can drain the final 1 MiB bundle after
ION stops transmission; the experimental service capacity remains the ION
contact rate, not the NetEm rate.

## Contact timeline

| Physical interval | ION interval with 4 s / 1 s guards | ION BP rate | Physical wire rate | Usable BP volume |
| --- | --- | ---: | ---: | ---: |
| `T0+90` to `T0+135` | `T0+94` to `T0+134` | 2 Mbit/s | 10 Mbit/s | 10,000,000 B |
| `T0+188` to `T0+233` | `T0+192` to `T0+232` | 1 Mbit/s | 10 Mbit/s | 5,000,000 B |
| `T0+278` to `T0+630` | `T0+282` to `T0+629` | 2 Mbit/s | 10 Mbit/s | Drain |

With 1 MiB payloads, the first two windows can carry approximately nine and
four complete payload bundles. For a 1 MiB payload, 100 ms bottleneck OWLT,
and one-second end guard, the minimum final-bundle drain rate is
`8 * 1,048,576 / (1 - 0.1) = 9.321 Mbit/s`; 10 Mbit/s is the smallest rounded
rate above that bound. BP overhead and six 3-byte `bpsendtest` pilot bundles
are recorded separately.

At 2 Mbit/s the 347-second recovery contact provides 86,750,000 bytes of
planned capacity, about 2.9 times the complete offered load.

## Cohorts

| Group | Injection | Priority | Payloads |
| --- | ---: | --- | ---: |
| A | `T0+30` | Bulk | 6 |
| B | `T0+30` | Standard | 6 |
| C | `T0+30` | Urgent | 6 |
| D | `T0+150` | Standard | 4 |
| E | `T0+150` | Urgent | 4 |
| F | `T0+330` | Standard control | 2 |

The guarded runner opens one `bpsendtest -rep` process per cohort, for six
sender attachments and 28 measured payloads per block. Run one accepted pilot
before the balanced 18-block, 504-payload study.
