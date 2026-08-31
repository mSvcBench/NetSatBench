# Native-clock ION handover with overlap

This scenario is the ION-DTN condition paired with the native IPv4 handover
benchmark. It preserves the 61-epoch size, one-minute handover cadence,
link-add timestamps, rates, delays, topology and `T0 + 30 s` traffic start.
Only link deletion is advanced by 24 seconds to implement the selected
6-second physical-overlap treatment. Native `ip route` oracle and `iperf3`
commands are omitted because forwarding is performed by BPv7, IPN, and CGR.

The eight inter-satellite ring links remain active.  User and ground access
links move once per minute using make-before-break:

```text
old physical access:  [start, start + 66 s]
new physical access:                [start + 60 s, ...]
physical overlap:                    6 s
```

With the experiment's 4-second contact-start guard and 1-second contact-end
guard, every completed transition has:

```text
ION overlap = 6 s - 4 s - 1 s = 1 s
```

The pilot injects one 4 MiB bundle at `T0 + 30 s`, matching the native
traffic-start timestamp.  Acceptance requires exact delivery size, matching
SHA-256 hashes, all 61 epochs, link readiness before every ION contact, zero
IP fragmentation counters, and zero critical UDP errors.

The two deletions at the start of the native cyclic trace describe links from
the preceding unrecorded cycle.  They have no effect on a fresh deployment and
are omitted so strict ION contact derivation never deletes an inactive link.

Run only the non-mutating preflight check with:

```bash
python3 labs/netsatbench/ion/analysis/run_handover_overlap_pilot.py \
  --preflight-only
```

Run the live pilot only after the native campaign has stopped.  The runner
refuses to replace containers while that campaign is active.
