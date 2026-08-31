# 01 - Store-Carry-Forward with Canonical Handover

Purpose: demonstrate DTN storage and forwarding across nine scheduled hops.
There is never a simultaneous end-to-end IP path from `usr1` to `grd1`.

Each next physical hop opens 6 seconds before the preceding hop closes. ION
uses a 4-second start guard and a 1-second end guard:

```text
old physical link:  [0, 45]
new physical link:      [39, 90]
physical overlap:       [39, 45] = 6 s
old ION contact:    [4, 44]
new ION contact:        [43, 89]
ION overlap:             [43, 44] = 1 s
```

The pattern repeats through:

```text
usr1-sat1, sat1-sat2, sat2-sat3, sat3-sat4, sat4-sat5,
sat5-sat6, sat6-sat7, sat7-sat8, sat8-grd1
```

The bundle is accepted only when the destination payload is complete and its
SHA-256 digest matches the source.
