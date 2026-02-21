# Fine grained QoS Control

NetSatBench manages link-level characteristics using Linux's traffic control (tc) utility, which operates on a per-interface basis. This means that all traffic traversing a given interface is subject to the same QoS settings. However, in some scenarios, it may be desirable to apply different QoS settings to different types of traffic, such as prioritizing user traffic over background traffic.
To support this use case, NetSatBench provides a fine-grained QoS control module that allows users to define custom traffic classes and apply different QoS settings to each class. This module is implemented in `extra/QoS/shaping-ns-create.sh`, for IPv4, and `extra/QoS/shaping-ns-create-v6.sh` for IPv6.

These scripts redirect redirects locally generated and forwarded traffic through a dedicated “shape” network namespace before reinjecting it into the root namespace. The indirection allows the application of fine-grained traffic shaping policies (e.g., tc qdisc, filters, classful scheduling) on veth0_rt without modifying the primary per link policy applied to the input/output satellite-to-x links.

It can be invoked from the “run” section of an epoch file via:
     `/app/extra/QoS/shaping-ns-create.sh`
when shaping is required, and later removed using:
    ` /app/extra/QoS/shaping-ns-delete.sh`

 Logical packet path:

   input_link
        ↓
     veth0_rt
        ↓
   [ veth0_ns → shape namespace → veth1_ns ]
        ↓
     veth1_rt
        ↓
    output_link

 In this architecture, the shape namespace acts as a controlled processing domain where traffic can be delayed, rate-limited, reordered, or otherwise manipulated before returning to the main routing context.