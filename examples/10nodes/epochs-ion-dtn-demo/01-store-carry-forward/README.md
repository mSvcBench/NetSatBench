# 01 - Store-Carry-Forward

Purpose: demonstrate the core DTN behavior.

Only one hop is open at a time:

```text
epoch0: usr1 <-> sat1
epoch1: sat1 <-> sat2
epoch2: sat2 <-> sat3
epoch3: sat3 <-> sat4
epoch4: sat4 <-> grd1
```

Expected result:

```text
usr1 sends the bundle.
sat1 receives and stores it.
sat2 receives and stores it.
sat3 receives and stores it.
sat4 receives and stores it.
grd1 receives the final payload.
```

There is never a simultaneous end-to-end IP path from `usr1` to `grd1`.
