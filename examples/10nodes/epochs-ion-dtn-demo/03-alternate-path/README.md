# 03 - Alternate Physical Path

Purpose: create a physical topology where two branches are available after
`sat1`.

Primary branch:

```text
usr1 -> sat1 -> sat2 -> sat3 -> sat4 -> grd1
```

Alternate branch:

```text
usr1 -> sat1 -> sat5 -> sat6 -> sat4 -> grd1
```

Epoch layout:

```text
epoch0: usr1 <-> sat1
epoch1: sat1 <-> sat2 and sat1 <-> sat5
epoch2: sat2 <-> sat3 is closed, sat5 <-> sat6 is open
epoch3: sat6 <-> sat4
epoch4: sat4 <-> grd1
```

The ION generator derives contacts and direct-neighbor plans from the scenario
links, so this scenario exercises the branch through `sat5` and `sat6`.

Expected observation:

```text
The bundle moves from usr1 to sat1, then through the available branch, and
reaches grd1 at epoch4.
```
