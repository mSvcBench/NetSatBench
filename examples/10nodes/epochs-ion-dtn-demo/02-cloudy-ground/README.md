# 02 - Cloudy Ground Station

Purpose: simulate a ground station outage.

The bundle moves toward `grd1`, but the final `sat4 <-> grd1` contact is
missing for two epochs:

```text
epoch0: usr1 <-> sat1
epoch1: sat1 <-> sat2
epoch2: sat2 <-> sat3
epoch3: sat3 <-> sat4
epoch4: no ground contact
epoch5: no ground contact
epoch6: sat4 <-> grd1
```

Expected result:

```text
sat4 stores the bundle during epoch4 and epoch5.
grd1 receives the bundle only when epoch6 opens the ground contact.
```

This is the cleanest scenario for explaining store-carry-forward with an
intermittent ground segment.
