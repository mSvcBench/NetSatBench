# ION-DTN Installation and Experiment Guide for NetSatBench

This guide starts from a clean Linux host and builds the ION-DTN-enabled
10-node experiment using NetSatBench as the only project workspace. The ION
source, staged runtime, Docker image definition, scenario files, and experiment
commands all remain inside the NetSatBench checkout.

The repository tests validate configuration generation and scenario
consistency. A successful experiment additionally requires the container,
VXLAN, ION-process, and bundle-delivery checks in this guide.

```text
NetSatBench/
|-- third_party/ION-DTN/          temporary ION source tree
|-- .local/ion/                   private host-side ION installation
|-- sat-container/ion-runtime/    ION files copied into the node image
|-- sat-container/Dockerfile.ion  ION-enabled NetSatBench node image
`-- examples/10nodes-ion-dtn/     self-contained 10-node ION-DTN example
    |-- sat-config.json           ION-enabled NetSatBench topology
    |-- worker-config.example.json versioned worker configuration template
    |-- worker-config.local.json   local worker settings, ignored by Git
    |-- ion/                      ION node registry and runbooks
    `-- epochs/                   NetSatBench contact scenarios
```

`third_party/ION-DTN`, `.local/ion`, and `sat-container/ion-runtime` are
reproducible build artifacts and are ignored by Git. No separate DTN lab
repository is required during the experiment.

## 1. How the integration works

```text
NetSatBench epoch files
        |
        v
VXLAN links + Linux routes + tc/netem
        |
        v
ION UDP convergence layer
        |
        v
Bundle Protocol v7 + IPN + CGR
```

NetSatBench controls when the emulated physical links open and close. ION uses
a contact plan generated from the same epoch files to send, store, and forward
bundles. During a CGR experiment, NetSatBench and ION must therefore use the
same absolute start time (`T0`).

The subsecond development line preserves epoch timestamps and numeric
`epoch-time` offsets to millisecond precision.  For example, a physical link
event at `T0+10.375s` is replayed at that offset rather than rounded to
`T0+10s`.  Fractional `--epoch-duration`, guard, start-delay, fixed-wait, and
loop-delay values are accepted to three decimal places.  The same line can
derive an ION range such as `0.005s` from a `4.902ms` NetEm delay by selecting
the `ceil-millisecond` OWLT policy.  Whole-second epoch files and the legacy
`ceil-positive` OWLT policy remain valid.

The initial application path is:

```text
usr1 -> sat1 -> sat2 -> sat3 -> sat4 -> sat5 -> sat6 -> sat7 -> sat8 -> grd1
```

## 2. Host requirements

The commands below target Debian or Ubuntu. For a single-host experiment, use
at least 4 CPU cores, 8 GiB of RAM, and approximately 15 GiB of free disk.

Required host capabilities:

- 64-bit Linux;
- Docker Engine with a running daemon;
- Python 3.10 or newer;
- an SSH server and key-based SSH access to each worker;
- passwordless access to the required `sudo` operations on each worker;
- Git, a C compiler, Make, and Autotools.

Install the packages:

```bash
sudo apt-get update
sudo apt-get install -y \
  git openssh-server docker.io \
  python3 python3-venv python3-pip \
  build-essential autoconf automake libtool m4 pkg-config \
  curl jq ca-certificates

sudo systemctl enable --now docker ssh
sudo usermod -aG docker "$USER"
```

Log out and back in after changing Docker group membership. Then verify:

```bash
docker version
python3 --version
ssh -V
```

NetSatBench worker initialization performs Docker and network operations.
The configured worker user must be able to run `sudo -n true` without a
password prompt. On shared or production hosts, grant only the commands that
NetSatBench requires instead of using an unrestricted NOPASSWD rule.

For a single-host lab, create an SSH key and authorize it locally:

```bash
test -f "$HOME/.ssh/id_ed25519_nsb" || \
  ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519_nsb"

touch "$HOME/.ssh/authorized_keys"
grep -qxF "$(cat "$HOME/.ssh/id_ed25519_nsb.pub")" \
  "$HOME/.ssh/authorized_keys" || \
  cat "$HOME/.ssh/id_ed25519_nsb.pub" >> "$HOME/.ssh/authorized_keys"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"

ssh -i "$HOME/.ssh/id_ed25519_nsb" localhost true
sudo -n true
```

Do not continue until both final commands succeed.

## 3. Clone and prepare NetSatBench

```bash
git clone --recurse-submodules https://github.com/mSvcBench/NetSatBench.git
cd NetSatBench
git submodule update --init --recursive
export NSB_ROOT="$PWD"
```

Unless noted otherwise, run every remaining command from `$NSB_ROOT`.

Create the Python environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
export NSB_PY="$NSB_ROOT/.venv/bin/python"

"$NSB_PY" ./nsb.py --help
```

After opening a new shell, restore these variables:

```bash
cd /path/to/NetSatBench
export NSB_ROOT="$PWD"
export NSB_PY="$NSB_ROOT/.venv/bin/python"
```

Confirm the working directory before using relative example paths:

```bash
cd "$NSB_ROOT"
test -f examples/10nodes-ion-dtn/worker-config.example.json
test -f examples/10nodes-ion-dtn/sat-config.json
```

If either check fails, correct `NSB_ROOT` first. This avoids misleading `jq:
Could not open file` and configuration-not-found errors.

## 4. Build ION-DTN inside NetSatBench

Clone ION under NetSatBench's `third_party` directory:

```bash
cd "$NSB_ROOT"
mkdir -p third_party
git clone https://github.com/nasa-jpl/ION-DTN.git third_party/ION-DTN

export ION_SRC="$NSB_ROOT/third_party/ION-DTN"
export ION_PREFIX="$NSB_ROOT/.local/ion"
```

Record the exact ION revision before building. To pin a release, inspect the
available tags and check out the selected tag before recording the commit:

```bash
cd "$ION_SRC"
git tag -l 'ion-open-source-*' | sort -V | tail
# Optional: git checkout ion-open-source-<version>
mkdir -p "$NSB_ROOT/.local"
git rev-parse HEAD | tee "$NSB_ROOT/.local/ion-source-commit.txt"
```

Apply the NetSatBench reliability patches before compiling. They retain the
`bpsendtest -rep` file reference and keep urgent queue ordinal state valid
when `bpclm` fragments a bundle:

```bash
cd "$ION_SRC"
"$NSB_ROOT/sat-container/apply-ion-patches.sh" "$ION_SRC"
```

Generated UDP outducts also set a 1200-byte maximum BP payload. This keeps
each serialized UDP CLA bundle below NetSatBench's 1408-byte VXLAN MTU, so the
kernel does not depend on IPv4 fragment reassembly across changing links. The
generated node configuration reserves 20,000,000 SDR heap words so the larger
number of BP fragments does not trigger ION heap protection. The UDP CLA
reliability patch requests a 4 MiB receive buffer for short fragment bursts.

Install ION into a private prefix inside NetSatBench. This avoids modifying
the system `/usr/local` installation:

```bash
cd "$ION_SRC"
test -x ./configure || autoreconf -fi
./configure --prefix="$ION_PREFIX"
make -j"$(nproc)"
make install
```

Verify all commands needed by the integration:

```bash
export PATH="$ION_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$ION_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

for cmd in ionadmin ionsecadmin bpadmin ipnadmin \
           bpsendfile bpsendtest bprecvfile bpstats bpwatch \
           udpcli udpclo killm; do
  command -v "$cmd" || { echo "Missing ION command: $cmd" >&2; exit 1; }
done
```

This integration uses BPv7, IPN, and the UDP convergence layer. BPSec and
MBEDTLS are not required for the basic experiment.

## 5. Stage ION and build the node image

The staging script copies the required ION executables and shared libraries
into `sat-container/ion-runtime`:

```bash
cd "$NSB_ROOT/sat-container"
ION_PREFIX="$ION_PREFIX" ./stage-ion-runtime.sh ion-runtime

test -x ion-runtime/bin/ionadmin
test -x ion-runtime/bin/bpsendfile
test -x ion-runtime/bin/bpsendtest
find ion-runtime/bin -maxdepth 1 -type f | sort
```

Build the ION-enabled NetSatBench node image:

```bash
docker build \
  -f "$NSB_ROOT/sat-container/Dockerfile.ion" \
  -t netsatbench-sat-container-ion:prototype \
  "$NSB_ROOT/sat-container"
```

Verify the image before deployment:

```bash
docker image inspect netsatbench-sat-container-ion:prototype >/dev/null
docker run --rm --entrypoint sh \
  netsatbench-sat-container-ion:prototype -lc '
    command -v ionadmin
    command -v bpadmin
    command -v bpsendfile
    command -v bpsendtest
    command -v udpclo
    ldconfig -p | grep -E "lib(bp|ici)\.so"
  '
```

Do not deploy if this reports a missing executable or shared library. Check
`ION_PREFIX`, the staging output, and `ion-runtime/lib` first.

## 6. Start Etcd

Determine an IPv4 address reachable from the worker and node containers:

```bash
export HOST_IP="$(ip -4 route get 1.1.1.1 | awk '{print $7; exit}')"
export ETCD_HOST="$HOST_IP"
export ETCD_PORT=2379
export NODE_ETCD_HOST="$HOST_IP"
export NODE_ETCD_PORT=2379

printf 'HOST_IP=%s\n' "$HOST_IP"
```

Set `HOST_IP` manually if automatic detection selects the wrong interface.
Never use `127.0.0.1` as `NODE_ETCD_HOST`; localhost inside a node container
refers to that container, not the Etcd host.

Start a single-node Etcd service:

```bash
docker volume create netsatbench-etcd-data
if docker container inspect etcd >/dev/null 2>&1; then
  echo "An Etcd container already exists; verify and reuse it."
  docker start etcd >/dev/null 2>&1 || true
  docker ps -a --filter name='^/etcd$'
else
docker run -d \
  --name etcd \
  --restart unless-stopped \
  -p 2379:2379 -p 2380:2380 \
  -v netsatbench-etcd-data:/etcd-data \
  quay.io/coreos/etcd:v3.5.17 \
  /usr/local/bin/etcd \
  --name nsb-etcd \
  --data-dir /etcd-data \
  --listen-client-urls http://0.0.0.0:2379 \
  --advertise-client-urls "http://$ETCD_HOST:2379" \
  --listen-peer-urls http://0.0.0.0:2380 \
  --initial-advertise-peer-urls "http://$ETCD_HOST:2380" \
  --initial-cluster "nsb-etcd=http://$ETCD_HOST:2380" \
  --initial-cluster-state new
fi
```

Verify it from the host:

```bash
curl --fail "http://$ETCD_HOST:$ETCD_PORT/version"
docker exec etcd etcdctl --endpoints=http://127.0.0.1:2379 endpoint health
```

If an Etcd service already exists, do not start another one or delete its
volume. Point the four Etcd variables at the existing endpoint. The conditional
above also avoids Docker's `container name /etcd is already in use` error.

## 7. Configure a single worker

Create a local worker configuration from the non-secret template, then replace
the user, SSH key, and host IP values:

```bash
if [ ! -e examples/10nodes-ion-dtn/worker-config.local.json ]; then
  cp examples/10nodes-ion-dtn/worker-config.example.json \
    examples/10nodes-ion-dtn/worker-config.local.json
fi
```

The resulting single-host configuration has this shape:

```json
{
  "workers-common": {
    "ssh-user": "__SSH_USER__",
    "ssh-key": "__SSH_KEY__",
    "sat-vnet": "sat-vnet",
    "sat-vnet-super-cidr": "172.20.0.0/16"
  },
  "workers": {
    "host-1": {
      "ip": "__HOST_IP__",
      "sat-vnet-cidr": "172.20.0.0/24",
      "cpu": "4",
      "mem": "8GiB"
    }
  }
}
```

Replace every placeholder, then validate JSON, SSH, Docker, and sudo access:

```bash
jq empty examples/10nodes-ion-dtn/worker-config.local.json

SSH_USER="$(jq -r '."workers-common"."ssh-user"' examples/10nodes-ion-dtn/worker-config.local.json)"
SSH_KEY="$(jq -r '."workers-common"."ssh-key"' examples/10nodes-ion-dtn/worker-config.local.json)"
WORKER_IP="$(jq -r '.workers["host-1"].ip' examples/10nodes-ion-dtn/worker-config.local.json)"
case "$SSH_USER $SSH_KEY $WORKER_IP" in
  *__*) echo "Replace every placeholder in worker-config.local.json" >&2; exit 1 ;;
esac
test -f "$SSH_KEY"
ssh -i "$SSH_KEY" "$SSH_USER@$WORKER_IP" \
  'docker version >/dev/null && sudo -n true'
```

The host management network, Docker underlay (`172.20.0.0/24`), and emulated
overlay networks (`172.100/101/102.0.0/16`) must not overlap.

## 8. Initialize the worker

If this worker previously ran NetSatBench nodes, detach them before recreating
the worker network:

```bash
"$NSB_PY" ./nsb.py reset --etcd-host "$ETCD_HOST" || true
printf 'yes\n' | "$NSB_PY" ./nsb.py rm -t 8 --etcd-host "$ETCD_HOST" || true
```

```bash
cd "$NSB_ROOT"
"$NSB_PY" ./nsb.py system-init-docker \
  --config examples/10nodes-ion-dtn/worker-config.local.json \
  --etcd-host "$ETCD_HOST"
```

Check the resulting Docker network and NetSatBench state:

```bash
docker network inspect sat-vnet >/dev/null
docker network inspect sat-vnet | jq '.[0].IPAM.Config'
"$NSB_PY" ./nsb.py status --etcd-host "$ETCD_HOST"
```

If initialization reports that `sat-vnet` has active endpoints, do not remove
the network forcibly. Run the reset/remove commands above, confirm that no
`sat*`, `usr*`, or `grd*` containers remain, and retry `system-init-docker`.

## 9. Distribute the ION image

On a single host, the image is already in the worker's Docker daemon:

```bash
docker image inspect netsatbench-sat-container-ion:prototype >/dev/null
```

For multiple workers, distribute the local image through NetSatBench:

```bash
"$NSB_PY" ./nsb.py load-image \
  --image netsatbench-sat-container-ion:prototype \
  --worker-config examples/10nodes-ion-dtn/worker-config.local.json \
  -t 4
```

Every worker must have the same tag because `sat-config.json` deliberately
uses `image-pull-policy: never`.

## 10. Initialize and deploy the ION scenario

If an earlier experiment exists, remove only that NetSatBench experiment:

```bash
"$NSB_PY" ./nsb.py reset --etcd-host "$ETCD_HOST" || true
printf 'yes\n' | "$NSB_PY" ./nsb.py rm -t 8 --etcd-host "$ETCD_HOST" || true
```

Load the ION image configuration and deploy the ten nodes:

```bash
"$NSB_PY" ./nsb.py init \
  --config examples/10nodes-ion-dtn/sat-config.json \
  --etcd-host "$ETCD_HOST" \
  --write-full-config

"$NSB_PY" ./nsb.py deploy -t 8 \
  --etcd-host "$ETCD_HOST" \
  --node-etcd-host "$NODE_ETCD_HOST"
```

Verify all nodes and ION tools:

```bash
"$NSB_PY" ./nsb.py status --etcd-host "$ETCD_HOST"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

for n in usr1 sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8 grd1; do
  docker exec "$n" sh -lc \
    'command -v ionadmin && command -v bpsendfile' || exit 1
done
```

`deploy` waits for each node to write its `eth0_ip` into Etcd. If it stalls,
use the Etcd diagnostics in the troubleshooting section.

## 11. Run the direct-neighbor VXLAN smoke test first

Apply the simple `00-all-open` topology using fixed waits:

```bash
"$NSB_PY" ./nsb.py run \
  --epoch-dir examples/10nodes-ion-dtn/epochs/00-all-open \
  --file-pattern 'NetSatBench-epoch*.json' \
  --fixed-wait 3 \
  --etcd-host "$ETCD_HOST"
```

Inspect direct-neighbor interfaces and routes:

```bash
docker exec usr1 ip -br link
docker exec usr1 ip route
docker exec sat1 ip route get 172.100.0.6
docker exec sat4 ip route get 172.100.0.18
docker exec sat8 ip route get 172.101.0.2
```

Do not proceed to ION/CGR until this layer works. `--fixed-wait` is only for
physical-topology diagnostics and must not be mixed with a timed CGR test.
This validates VXLAN and direct `/32` neighbor routes only. The ION profile
disables multi-hop IP routing, so `usr1 -> grd1` IP reachability is not a
success condition. For a complete ION `00-all-open` test, repeat Steps 12
through 15 with that scenario and send one bundle.

Clear runtime link state before the timed experiment:

```bash
"$NSB_PY" ./nsb.py reset --etcd-host "$ETCD_HOST"
```

## 12. Generate ION configuration and start ION

List the available scenarios:

```bash
"$NSB_PY" ./nsb.py ion-prepare --list-scenarios
```

Use `02-cloudy-ground` for the first store-carry-forward demonstration. Create
a future absolute start time shared by ION and NetSatBench:

```bash
export T0="$(date -u -d '+3 minutes' '+%Y-%m-%dT%H:%M:%SZ')"
printf 'Experiment T0: %s\n' "$T0"

"$NSB_PY" ./nsb.py ion-prepare \
  --scenario 02-cloudy-ground \
  --epoch-duration 45 \
  --contact-start-guard 4 \
  --contact-end-guard 1 \
  --absolute-start-time "$T0" \
  --jobs 8
```

`ion-prepare` performs the following operations:

1. derives bidirectional ION ranges and contacts from the epoch files;
2. generates `.ionrc`, `.ionsecrc`, `.bprc`, and `.ipnrc` files in a temporary
   host directory;
3. copies only the required files into `/ion` in each participating node;
4. stops stale ION processes and starts a new ION instance in every node;
5. removes the temporary generated configuration from the host.

The guards keep the planned ION contact inside the physical link window:
Each replacement physical link starts 6 seconds before the old link closes. ION starts 4 seconds after link creation and stops 1 second before removal, leaving exactly 1 second of ION overlap. Record the selected values
with experiment results; they change the effective contact duration.

Verify configuration and processes:

```bash
for n in usr1 sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8 grd1; do
  echo "=== $n ==="
  docker exec "$n" sh -lc '
    test -f /ion/ion_nodes
    test -f /ion/'"$n"'/node.ionrc
    pgrep -a rfxclock
    pgrep -a bpclock
    tail -n 20 /ion/'"$n"'/ion.log
  ' || exit 1
done
```

## 13. Start the receiver and enqueue a bundle

Start the BP receiver on `grd1` before `T0`:

```bash
docker exec grd1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/grd1
  rm -f testfile* receiver.log
  pkill -x bprecvfile >/dev/null 2>&1 || true
  nohup bprecvfile ipn:110.1 1 > receiver.log 2>&1 &
  sleep 1
  pgrep -a bprecvfile
'
```

Enqueue a bundle at the `usr1` source endpoint:

```bash
docker exec usr1 sh -lc '
  export ION_NODE_LIST_DIR=/ion
  cd /ion/usr1
  printf "NetSatBench ION-DTN payload %s\n" "$(date -u +%FT%TZ)" > payload.txt
  bpsendfile ipn:101.1 ipn:110.1 payload.txt 0.1.0 1800
'
```

Sending before `T0` is intentional: ION stores the bundle until an applicable
contact starts.

## 14. Replay NetSatBench epochs with the same T0

Open another terminal and reuse the exact `T0` printed in the first terminal:

```bash
cd /path/to/NetSatBench
export NSB_ROOT="$PWD"
export NSB_PY="$NSB_ROOT/.venv/bin/python"
export ETCD_HOST="<same-etcd-host>"
export T0="<exact-T0-from-ion-prepare>"

"$NSB_PY" ./nsb.py run \
  --epoch-dir examples/10nodes-ion-dtn/epochs/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch*.json' \
  --absolute-start-time "$T0" \
  --etcd-host "$ETCD_HOST"
```

If `T0` has already passed, do not reuse the stale plan. Return to Step 12 and
prepare ION with a new future value. Different ION and NetSatBench start times
disconnect CGR decisions from the emulated physical links.

## 15. Verify delivery and store-carry-forward

Check the delivered application file:

```bash
docker exec grd1 sh -lc '
  cd /ion/grd1
  ls -la testfile* receiver.log 2>/dev/null || true
  cat testfile1 2>/dev/null || true
'
```

Inspect ION logs across the path:

```bash
for n in usr1 sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8 grd1; do
  echo "=== $n ==="
  docker exec "$n" sh -lc \
    "grep -E 'rcv|fwd|xmt|reforward|enqueue|error' /ion/$n/ion.log | tail -n 80 || true"
done
```

| Signal | Meaning |
| --- | --- |
| `rcv` | The node received a bundle. |
| `fwd` | ION selected the bundle for forwarding. |
| `xmt` | The convergence layer transmitted the bundle. |
| `testfileN` | `bprecvfile` delivered a complete bundle to the application. |
| `bpacq.*` | Usually an incomplete acquisition when a contact closed early. |

In `02-cloudy-ground`, the expected behavior is that the bundle reaches
`sat8`, remains stored while `sat8 <-> grd1` is unavailable, and is delivered
after that contact opens.

Inspect the physical-link state in Etcd if necessary:

```bash
docker exec etcd etcdctl --endpoints=http://127.0.0.1:2379 \
  get /config/links --prefix --keys-only | sort
```

Finish with explicit delivery and health assertions. The Test 03 study runner
uses one receiver directory per group; the manual scenarios use `/ion/grd1`
directly. Set `EXPECTED_FILES=3` for scenario `04` and `1` for the other
manual scenarios. The nine scenario `03` probes and their enqueue offsets are
defined in that scenario's README:

```bash
EXPECTED_FILES=1
DELIVERED="$(docker exec grd1 sh -lc "find /ion/grd1 -maxdepth 1 -type f -name 'testfile*' | wc -l")"
HEALTHY="$(docker ps --filter health=healthy --format '{{.Names}}' | grep -E '^(sat[1-8]|usr1|grd1)$' | wc -l)"
test "$DELIVERED" -ge "$EXPECTED_FILES"
test "$HEALTHY" -eq 10
printf 'delivered=%s healthy=%s\n' "$DELIVERED" "$HEALTHY"
```

## 16. Troubleshooting

### Docker cannot find `ion-runtime`

Complete Steps 4 and 5 before building the image:

```bash
test -x "$NSB_ROOT/sat-container/ion-runtime/bin/ionadmin"
test -d "$NSB_ROOT/sat-container/ion-runtime/lib"
```

The Docker build context must be `sat-container`.

### ION does not compile

Check the Autotools dependencies and the first configure error:

```bash
cd "$ION_SRC"
autoreconf -fi
./configure --prefix="$ION_PREFIX"
```

Do not mix different ION build systems in a dirty source tree. Recreate the
`third_party/ION-DTN` checkout if it previously used another build method.

### Deploy waits for `eth0_ip`

Test Etcd from both the host and a node:

```bash
curl --fail "http://$ETCD_HOST:$ETCD_PORT/version"
docker exec sat1 sh -lc \
  "curl --fail --max-time 3 http://$NODE_ETCD_HOST:$NODE_ETCD_PORT/version"
docker logs sat1 --tail 100
```

If the management IP is unreachable from a single-host container, find the
actual `sat-vnet` gateway:

```bash
docker network inspect sat-vnet | jq -r '.[0].IPAM.Config[0].Gateway'
```

Use that address as `NODE_ETCD_HOST`, allow TCP/2379 through the firewall, and
retry with `deploy --fix`.

### The ION image is missing or being pulled

```bash
docker image inspect netsatbench-sat-container-ion:prototype
jq '.. | objects | select(has("image")) | .image' \
  examples/10nodes-ion-dtn/sat-config.json
```

Repeat the `load-image` step for every worker in a multi-worker deployment.

### `ion-prepare` cannot find a container

`init` and `deploy` must finish first:

```bash
docker ps --format '{{.Names}}' | sort
```

At minimum, the scenario's user, satellites, and ground node must be running.

### ION logs `xmt`, but the neighbor receives nothing

Check the direct overlay route and VXLAN interface:

```bash
docker exec sat1 ip route get 172.100.0.6
docker exec sat1 ip -br link
docker exec sat1 tcpdump -ni any udp port 4556
```

The node agent must install direct `/32 onlink` routes for VXLAN neighbors.
Fix the NetSatBench link/routing layer before changing ION configuration.

### No bundle is delivered

Check in this order:

1. Does the `00-all-open` direct-neighbor VXLAN smoke test pass?
2. Are `rfxclock`, `bpclock`, `ipnfw`, `udpcli`, and `udpclo` running?
3. Did the `ipn:110.1` receiver start before bundle transmission?
4. Did `ion-prepare` and `run` use the exact same future `T0`?
5. Is the bundle TTL long enough to cover the wait before `T0` and the complete
   scenario duration?
6. Do `/ion/<node>/ion.log` files report a configuration or socket error?

### Only a small bundle passes the short-contact test

`05-short-contact-capacity` is a capacity-boundary scenario. A large bundle
may leave `bpacq.*` on `grd1`, which can be a valid result. Increase the contact
window or `--epoch-duration` and compare the outcome.

### Epoch files disappear or change during a run

`nsb.py run` reads each epoch file when its scheduled time arrives. Do not
move, regenerate, or edit the active scenario directory until the run ends. If
a file-not-found or changed-schedule error occurs, keep the log, restore a
stable scenario directory, perform a clean `reset -> rm -> init -> deploy`, and
restart with a new future `T0`.

## 17. Available scenarios

| Scenario | Epochs / approximate run | Send / expected delivery | Purpose |
| --- | ---: | ---: | --- |
| `00-all-open` | 1 / 45 s | 1 / 1 | Basic ION forwarding with every hop open. |
| `01-store-carry-forward` | 17 / 6 min 45 s | 1 / 1 | Opens one subsequent hop per epoch. |
| `02-cloudy-ground` | 17 / approximately 8 min 9 s | 1 / 1 | Delays the final ground contact. |
| `03-alternate-path` | 10 / 7 min 30 s | 9 / 9 | Tests a symmetric CGR rate sweep, outage, recovery, and queue drain on one orbital ring. |
| `04-multi-bundle-queue` | 7 / 7 min 30 s | 28 payloads/block | Tests priority-aware service through two constrained bottleneck contacts. |
| `05-short-contact-capacity` | 5 / about 40 s | 2 / only the 10 KiB positive control | Tests a guarded contact-capacity boundary with a 24 MiB negative control. |

The approximate run includes the final contact-closing duration but not the
future-T0 preparation buffer. Epoch timestamps remain authoritative.

When changing scenarios, keep the scenario directory, contact generation, and
absolute time aligned:

```bash
SCENARIO=01-store-carry-forward
EPOCH_DURATION=45
CONTACT_START_GUARD=4
CONTACT_END_GUARD=1

"$NSB_PY" ./nsb.py reset --etcd-host "$ETCD_HOST"
printf 'yes\n' | "$NSB_PY" ./nsb.py rm -t 8 --etcd-host "$ETCD_HOST"
"$NSB_PY" ./nsb.py init \
  --config examples/10nodes-ion-dtn/sat-config.json \
  --etcd-host "$ETCD_HOST"
"$NSB_PY" ./nsb.py deploy -t 8 \
  --etcd-host "$ETCD_HOST" \
  --node-etcd-host "$NODE_ETCD_HOST"

T0="$(date -u -d '+3 minutes' '+%Y-%m-%dT%H:%M:%SZ')"
"$NSB_PY" ./nsb.py ion-prepare \
  --scenario "$SCENARIO" \
  --epoch-duration "$EPOCH_DURATION" \
  --contact-start-guard "$CONTACT_START_GUARD" \
  --contact-end-guard "$CONTACT_END_GUARD" \
  --absolute-start-time "$T0" \
  --jobs 8

# Start the receiver and enqueue the expected bundle(s) as shown in Step 13.

"$NSB_PY" ./nsb.py run \
  --epoch-dir "examples/10nodes-ion-dtn/epochs/$SCENARIO" \
  --file-pattern 'NetSatBench-epoch*.json' \
  --absolute-start-time "$T0" \
  --etcd-host "$ETCD_HOST"
```

Use `EPOCH_DURATION=10` for `05-short-contact-capacity`; the same canonical
4-second start and 1-second end guards leave a positive contact window.

For a distributed deployment, add `--transport ssh --etcd-host "$ETCD_HOST"`
to `ion-prepare`. The command resolves every node's assigned worker from Etcd,
copies a node-specific configuration archive over SCP, and runs the container
preparation commands on that worker. The default `auto` transport retains local
Docker behavior when a selected container is present on the control host.

For `04-multi-bundle-queue`, use six distinct source and destination service
endpoints. Each cohort is sent by one `bpsendtest -rep` invocation:

```bash
# QoS priority is the second component: 0=bulk, 1=standard, 2=urgent.
bpsendtest -rep 6 -ttl 1800 -qos 0.0.0 ipn:101.11 ipn:110.11 queue-A.bin
bpsendtest -rep 6 -ttl 1800 -qos 0.1.0 ipn:101.12 ipn:110.12 queue-B.bin
bpsendtest -rep 6 -ttl 1800 -qos 0.2.0 ipn:101.13 ipn:110.13 queue-C.bin
```

The quantitative runner in the companion implementation lab schedules all six
cohorts, separates the 3-byte pilot bundles, validates SDR before and after
each block, and requires an accepted one-block pilot before the 18-block
study.

For `05-short-contact-capacity`, the companion lab runner integrates the
guarded rate timeline to `20,125,000` bytes and then queues a 10 KiB positive
control plus a 24 MiB capacity negative control:

```bash
python labs/netsatbench/ion/analysis/run_short_contact_capacity.py \
  --etcd-host "$ETCD_HOST"
```

The equivalent manual payload setup is:

```bash
docker exec grd1 sh -lc 'export ION_NODE_LIST_DIR=/ion; cd /ion/grd1; nohup bprecvfile ipn:110.1 2 > receiver.log 2>&1 &'
docker exec usr1 sh -lc '
  export ION_NODE_LIST_DIR=/ion; cd /ion/usr1
  dd if=/dev/urandom of=payload-small.bin bs=10240 count=1 status=none
  bpsendfile ipn:101.1 ipn:110.1 payload-small.bin 0.1.0 900
  dd if=/dev/zero of=payload-large.bin bs=1M count=24 status=none
  bpsendfile ipn:101.1 ipn:110.1 payload-large.bin 0.1.0 900
'
```

After every scenario, require a zero `nsb.py run` exit status, ten healthy node
containers, the expected `testfileN` count, and matching ION `fwd`/`xmt`/`rcv`
events. Before the next scenario use `reset -> rm -> init -> deploy`; raw Docker
restarts can leave stale VXLAN endpoints or routes.

## 18. Generate configuration manually

Normal experiments should use `ion-prepare`. To inspect the generated files:

```bash
"$NSB_PY" ./nsb.py ion-generate-configs \
  --registry examples/10nodes-ion-dtn/ion/ion-node-registry.json \
  --output examples/10nodes-ion-dtn/ion/generated \
  --epoch-dir examples/10nodes-ion-dtn/epochs/02-cloudy-ground \
  --file-pattern 'NetSatBench-epoch*.json' \
  --epoch-duration 45 \
  --contact-start-guard 4 \
  --contact-end-guard 1 \
  --absolute-start-time "$T0"

find examples/10nodes-ion-dtn/ion/generated -maxdepth 2 -type f | sort
```

`generated/` is disposable and ignored by Git. Prefer changing
`ion-node-registry.json`, the epoch files, or the generator instead of maintaining
hand-edited generated configurations.

## 19. Stop and clean the experiment

Stop ION cleanly before removing the nodes:

```bash
for n in usr1 sat1 sat2 sat3 sat4 sat5 sat6 sat7 sat8 grd1; do
  docker inspect "$n" >/dev/null 2>&1 || continue
  docker exec "$n" sh -lc '
    export ION_NODE_LIST_DIR=/ion
    ionstop >/dev/null 2>&1 || true
    ionexit >/dev/null 2>&1 || true
    killm >/dev/null 2>&1 || true
  '
done
```

Clear only runtime links and run commands:

```bash
"$NSB_PY" ./nsb.py reset --etcd-host "$ETCD_HOST"
```

Remove the emulated node containers:

```bash
"$NSB_PY" ./nsb.py rm -t 8 --etcd-host "$ETCD_HOST"
```

Remove worker-side NetSatBench networking when it is no longer needed:

```bash
"$NSB_PY" ./nsb.py system-clean-docker \
  --config examples/10nodes-ion-dtn/worker-config.local.json \
  --etcd-host "$ETCD_HOST"
```

The Etcd volume and ION image remain persistent. Remove them separately only
when their data is no longer needed. The ION source, private installation, and
staged runtime can always be regenerated.

## 20. Run the repository validation tests

The tests use Python's standard `unittest` runner; `pytest` is not required:

```bash
cd "$NSB_ROOT"
"$NSB_PY" -m unittest discover -s test -p 'test_ion*.py' -v
"$NSB_PY" -m unittest discover -s test -p 'test_nsb_ion*.py' -v
```

They validate all seven scenarios, the ten-node registry, checked-in epochs,
rate updates, and generated contacts. They do not start Docker, replay a live
epoch, or prove end-to-end bundle delivery; use Steps 10 through 15 for that.

## 21. Multi-worker requirements

The experiment flow remains the same, but every worker must satisfy all of the
following requirements:

- use a unique, non-overlapping `sat-vnet-cidr`;
- permit key-based SSH and the required passwordless sudo operations;
- contain the ION image distributed by `nsb.py load-image`;
- reach the Etcd private address from the host and its containers;
- have routes to every other worker's container underlay subnet;
- allow UDP/4789 for VXLAN and TCP/2379 for Etcd;
- permit container-source traffic in cloud anti-spoofing or
  allowed-address-pair policies.

A healthy container alone does not prove that the experiment is ready. Verify
Etcd reachability, `eth0_ip` registration, VXLAN links, overlay routes, ION
processes, and end-to-end `testfileN` delivery.
