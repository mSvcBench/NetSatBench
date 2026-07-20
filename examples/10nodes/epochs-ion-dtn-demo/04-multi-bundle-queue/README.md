# 04 - Multi-Bundle Queue

Purpose: send more than one bundle while contacts are intermittent.

Recommended manual timing:

```text
epoch0: send Bundle A from usr1
epoch1: send Bundle B from usr1 after sat1-sat2 opens
epoch2: no useful new contact, observe waiting
epoch3: sat2-sat3 opens
epoch4: sat3-sat4 opens
epoch5: sat4-grd1 opens
epoch6: keep sat4-grd1 open for late delivery checks
```

Expected result:

```text
Bundles do not all arrive at the same time.
Relay logs should show different receive/forward times.
```

This is useful for observing ION queueing and checking whether later bundles
catch up or wait behind earlier traffic.
