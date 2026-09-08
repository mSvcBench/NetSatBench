# 02 - Cloudy Ground Station

Purpose: add a controlled outage before the final `sat8-grd1` service window.
The first eight relay hops use the same canonical 6-second physical overlap,
4-second start guard, and 1-second end guard as Test 01.

The nominal final handover is:

```text
sat7-sat8 physical: [309, 360]
sat8-grd1 physical: [354, ...]
physical overlap:   [354, 360] = 6 s
sat7-sat8 ION:      [313, 359]
sat8-grd1 ION:      [358, ...]
ION overlap:         [358, 359] = 1 s
```

The experimental groups add 0, 45, 90, or 180 seconds to the final ground-link
start. Positive outage treatments deliberately replace the nominal overlap
with a controlled gap while leaving the 4/1 contact guards unchanged. During
that gap, `sat8` must retain the complete bundle and deliver it when the ground
contact becomes available.
