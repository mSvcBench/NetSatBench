# 03 - Single-Orbit Ring Path Reselection

Purpose: verify that ION-CGR can select the opposite direction around one
orbital ring when an inter-satellite link on the preferred direction is
degraded or unavailable.

The eight satellites belong to `sat_orbit: 1` in `sat-config.json`. This
scenario connects only adjacent positions in that orbit:

```text
             sat2 --- sat3 --- sat4
            /                    \
usr1 --- sat1                    sat5 --- grd1
            \                    /
             sat8 --- sat7 --- sat6
```

The two `sat1 -> sat5` routes are:

```text
clockwise, preferred:       sat1 -> sat2 -> sat3 -> sat4 -> sat5
counterclockwise, alternate: sat1 -> sat8 -> sat7 -> sat6 -> sat5
```

Clockwise links initially use 10 Mbit/s. Counterclockwise links remain at
5 Mbit/s. The `sat2 <-> sat3` rate is swept below the alternate-path rate,
removed, and then restored through the same rates in reverse order. This
symmetric design tests both the selection threshold and route hysteresis.

| Group | Epoch | Probe offset | `sat2 <-> sat3` | Expected route |
| --- | ---: | ---: | ---: | --- |
| A | 0 | `T0 + 15 s` | 10 Mbit/s | Clockwise |
| B | 1 | `T0 + 60 s` | 3 Mbit/s | Clockwise |
| C | 2 | `T0 + 105 s` | 1 Mbit/s | Counterclockwise |
| D | 3 | `T0 + 150 s` | 100 Kbit/s | Counterclockwise |
| E | 4 | `T0 + 195 s` | Closed | Counterclockwise |
| F | 5 | `T0 + 240 s` | 100 Kbit/s | Counterclockwise |
| G | 6 | `T0 + 285 s` | 1 Mbit/s | Counterclockwise |
| H | 7 | `T0 + 330 s` | 3 Mbit/s | Clockwise |
| I | 8 | `T0 + 375 s` | 10 Mbit/s | Clockwise |

Epoch 9 sends no probe. It retains the restored 10 Mbit/s link for one
additional 45-second drain interval so that the final contact plan does not
censor the last probe or previously queued bundles.

The crossover is not the alternate path's 5 Mbit/s link rate. With equal
six-hop OWLT, the empty-queue serialization terms are:

```text
clockwise:        B * (5 / 10 + 1 / Rprimary)
counterclockwise: B * (2 / 10 + 4 / 5)
```

Rates are in Mbit/s and `B` is the bundle size in Mbit. Equating the terms
gives `Rprimary = 2 Mbit/s`. Therefore 3 Mbit/s remains faster clockwise,
whereas 1 Mbit/s is faster counterclockwise. ION's exact projected bundle
arrival time uses estimated bundle volume, contact-capacity reservations, and
integer radiation latencies. In addition, `bpsendfile` can represent one
application file as multiple bundles. The file-level crossover is therefore a
falsifiable baseline, not an assertion about the route ION must select.

Use the same 1 MiB payload in all nine groups. Each group has a distinct BP
service endpoint and receiver directory, so late delivery cannot be
misattributed to the next probe. With the canonical 4-second start guard and 1-second end guard, the descending
100 Kbit/s segment carries at most 550,000 bytes and the restored segment
carries at most 512,500 bytes. These bounds
test bundle-level CGR capacity accounting when the complete application file
does not fit in one primary-link contact.

Repeat the complete nine-probe, ten-epoch sequence 15 times. Each repetition
is one experimental block, producing 135 probe observations. Compare the
symmetric rate pairs within each block:

```text
A vs I: 10 Mbit/s before degradation vs after recovery
B vs H: 3 Mbit/s descending vs ascending
C vs G: 1 Mbit/s descending vs ascending
D vs F: 100 Kbit/s descending vs ascending
```

These pairs reveal persistent recovery cost or path-selection hysteresis.
Group E isolates complete preferred-link unavailability.

Do not infer route selection from delivery alone. Retain hop-level UDP or ION
forwarding evidence and classify each delivered probe as:

```text
clockwise:        sat1, sat2, sat3, sat4, sat5
counterclockwise: sat1, sat8, sat7, sat6, sat5
```

This is a deterministic, epoch-driven ring emulation. It does not propagate
orbital positions or detect an unplanned physical failure. ION receives the
complete contact plan before `T0`, so this scenario tests planned,
rate-aware route selection rather than runtime failure detection.
