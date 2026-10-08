# 07 - Millisecond Scale

Purpose: find where contact dispatch stops being trustworthy, by compressing
`05-short-contact-capacity` onto a millisecond timeline.

This is a stress scenario, not a physically realistic one. Orbital mechanics
produce passes lasting seconds to minutes; a 180 ms contact does not occur in
flight. Use `08-millisecond-precision` for the realistic case and this one to
document the limit.

```text
epoch0 at +0      usr1 -> sat1 -> sat2 -> grd1 path opens at 10 Mbit/s
epoch1 at +300ms  sat1-sat2 drops to 1 Mbit/s
epoch2 at +600ms  sat1-sat2 drops to 100 Kbit/s
epoch3 at +900ms  sat1-sat2 recovers to 10 Mbit/s
epoch4 at +1200ms all links close
```

## Why the rates are unchanged

Compressing time by a factor `k` allows two policies. Preserving contact
volume would require multiplying every rate by `k`, which at `k = 33` asks the
VXLAN/NetEm data plane for bandwidth far beyond the ~9.5 Mbit/s the campaigns
actually measured. This scenario therefore holds rates constant and lets the
transferable volume scale down with time, so the ordering and count of protocol
events are preserved while the timing shrinks.

The guarded bottleneck volume is 603,750 B, exactly 1/33 of the 20,125,000 B
of the second-scale original.

## Choosing the compression factor

Dispatch error is set by the `rfxclock` poll interval and is independent of
contact length; measured at a 10 ms poll it is 7 ms median and 14 ms worst
case. The error therefore grows as a *fraction* of the contact as the contact
shrinks:

```text
contact   worst-case error / contact
  20 s                       0.0 %
   2 s                       0.6 %
 700 ms                      1.7 %
 300 ms                      4.3 %
 150 ms                      8.0 %
  40 ms                     30.0 %
```

A sweep of 5 repetitions at each of those lengths completed the
`0 -> rate -> 0` transition in all 40 runs, so the mechanism itself does not
break at 40 ms. What degrades is accuracy, not function: at 40 ms the contact
opens or closes up to 30 % late, which makes any capacity calculation built on
it meaningless. Holding the error below 5 % of the contact puts the practical
floor near 300 ms, which is why `k = 33` was chosen rather than 100.

## Enabling millisecond dispatch

Set `ION_RFXCLOCK_POLL_MSEC` per node in the satellite configuration before
deploying; see `08-millisecond-precision/README.md`. Without it ION polls once
per second and a 180 ms contact is missed entirely.
