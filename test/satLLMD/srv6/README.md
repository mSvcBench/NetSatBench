# SRv6 reverse-path auto-learning

Helper scripts that let users and satellites exchange traffic over SRv6 **without any central
route computation**: the user picks an access satellite, and the **end node** of the SRv6 path
(the node where the SRH is exhausted) learns the way back from the packet itself. Intermediate
nodes only forward and learn nothing. A third script steers the traffic of the llm-d service SID
to the gateway of the access satellite's orbit.

| Script | Runs on | Role |
|---|---|---|
| [usr_srv6_learn.py](usr_srv6_learn.py) | user nodes | Chooses the access satellite and installs an SRv6 default route through it. Follows handovers. Writes the access satellite (name, IPv6, orbit) to a local file. |
| [sat_srv6_learn.py](sat_srv6_learn.py) | the end node of the SRv6 path (the node that must answer) | Sniffs incoming SRv6 packets that end here and installs a `/128` reverse route towards each sender. |

## How it works

1. **User side.** The user watches its links in Etcd (`/config/links/<node>/`). When a satellite
   is in range it becomes the access satellite, and the script installs

   ```
   ip -6 route replace default encap seg6 mode inline segs <access-sat-ipv6> dev sr0
   ```

   Every packet leaving the user then carries an SRH whose first segment is the access
   satellite. The satellite IPv6 address is taken from `/etc/hosts`.
   When the access link disappears (Etcd delete event) or is about to expire, the script
   re-selects a satellite and replaces the default route. At each change it also writes the
   access satellite to a local file (see [Access file](#access-file)).
2. **Forward path.** Packets travel through the network along the SRH segment list towards the
   destination. Intermediate nodes do normal SRv6 processing.
3. **Learning.** Only on the end node, where the SRH is exhausted (`segments_left == 0`), the
   learner takes the SRH entries `Address[1..n-1]` (`Address[0]` is the node itself), i.e. the
   forward waypoints in reverse order, and installs:

   ```
   ip -6 route replace <src>/128 encap seg6 mode inline segs <reverse list> dev sr0
   ```

   Replies to `<src>` therefore follow the reverse of the path the request took, with no routing
   protocol and no controller.

   A source is not learned when the route the node actually uses to reach it (`ip -6 route get`, default
   route included) has a link-local next hop (`via fe80::...`). `encap seg6` routes do not count. The
   check is repeated at most every 10 s per source, and the first time a source is skipped the log shows
   `not learning <src>: route via link-local next hop (...)`.
4. **Ageing.** Every `--ttl-refresh` seconds, a learned route is deleted if its source has sent no traffic
   for `--ttl` seconds. All learned routes are removed on `SIGINT`/`SIGTERM`.

The routes are attached to a permanent placeholder dummy interface (`sr0`, MTU 1400 to leave room
for the SRH). If the dummy device cannot be created, `eth0` is used instead.

#### Access file

Written atomically by `usr_srv6_learn.py` (default `/tmp/srv6_access.json`, option
`--access-file`). The orbit is read from `/config/nodes/<sat>` in Etcd
(`metadata.sat_config.sat_orbit`).

```json
{"status": "connected", "satellite": "sat299", "ipv6": "2001:db8:100::4a9", "orbit": 7, "updated": 1731750000.0}
```

`status` is `not_connected` (with empty `satellite`/`ipv6` and null `orbit`) when there is no
access satellite.


## Requirements

- Linux with SRv6 support (`seg6` encap, `dummy` module recommended).
- Run inside the node's network namespace with `NET_ADMIN` (NetSatBench containers are
  `--privileged`).
- Python 3. `sat_srv6_learn.py` uses only the standard library. `usr_srv6_learn.py` needs
  `etcd3`.
- User script: environment variables `NODE_NAME`, `ETCD_ENDPOINT` (default `127.0.0.1:2379`),
  and optionally `ETCD_USER`, `ETCD_PASSWORD`, `ETCD_CA_CERT`. These are set by `nsb-deploy` /
  `nsb-node-restart`.
- The IPv6 addresses of the remote endpoints must be present in `/etc/hosts`.

## Usage

### End node (the node that answers)

```bash
python3 sat_srv6_learn.py --prefix 2001:db8:102::/48 --route-mtu 1300 --ttl 300
```

| Option | Default | Description |
|---|---|---|
| `--prefix` | none | Learn only sources inside this prefix. |
| `--exclude-iface` | `eth0` | Ignore packets received on this interface (empty = none). |
| `--dev` | creates `sr0` | Placeholder interface for the learned routes. |
| `--route-mtu` | none | MTU of learned routes; leave room for the SRH. |
| `--ttl` | `300` | Seconds without traffic before a route is removed. |
| `--ttl-refresh` | `10` | Period (s) of the check that removes expired routes (not done at every packet). A route is removed at most `ttl + ttl-refresh` s after its last packet. |

Link-local and multicast sources are never learned. Log lines look like
`learned <src> via <seg1,seg2,...> (from <ifindex>)` and `expired <src>`.

### User

```bash
python3 usr_srv6_learn.py --handover-strategy lifetime \
    --handover-metadata '{"lifetime_threshold_s": 60}' --log-level INFO
```

| Option | Default | Description |
|---|---|---|
| `--handover-strategy` | `lifetime` | Access-satellite selection strategy. Only `lifetime` is available: keep the link with the longest remaining lifetime. |
| `--handover-metadata` | `{}` | JSON passed to the strategy. `lifetime_threshold_s` is the remaining lifetime below which a handover is triggered (default: a quarter of the initial link duration). |
| `--link-duration-initial-value` | `240` | Assumed duration (s) of links without an `expected_duration` in Etcd. |
| `--dummy-dev-name` | `sr0` | Placeholder interface for the SRv6 default route. |
| `--access-file` | `/tmp/srv6_access.json` | Local file where the current access satellite (name, IPv6, orbit) is written. |
| `--log-level` | `INFO` | Logging level. |

The remaining lifetime of a link is `expected_duration` (from the link entry in Etcd, if present)
minus the time since the link appeared.


## Quick check

On the end node, after the user has sent some traffic:

```bash
ip -6 route show | grep seg6      # learned /128 routes
```

On the user:

```bash
ip -6 route show default          # default encap seg6 ... segs <access-sat>
cat /tmp/srv6_access.json         # current access satellite and orbit
```

## Limitations

- The learner only recognises an SRH placed directly after the IPv6 header (the BPF filter
  checks fixed offsets); packets with other extension headers before the SRH are ignored.
- Routes are keyed on the source address only, so one source has a single reverse path at a time
  (the most recent one wins).
- Learning requires traffic from the peer first: a node cannot reach a destination it has never
  received an SRv6 packet from, unless a route already exists.
