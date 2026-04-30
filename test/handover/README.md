# Handover SRv6 Use Case

This document describes how to prepare data, deploy emulation, apply oracle routing, and run handover tests (ping and iperf3).

## 1. Generate constellation `.h5` data

Configure satellites, gateways, and users for `<constellation>` in:

- `generators/StarPerf_Simulator/config/XML_constellation/<constellation>.xml`
- `generators/StarPerf_Simulator/config/ground_stations/<constellation>.xml`
- `generators/StarPerf_Simulator/config/users/<constellation>.xml`

Example for `<constellation> = OneWeb`:

```bash
./nsb.py starperf-generate \
--constellation OneWeb \
--dT 5 \
--isl-connectivity-plugin positive_Grid \
--gs-antenna-plugin pass_antenna \
--user-antenna-plugin pass_antenna \
--isl-rate-plugin pass_rate \
--gs-rate-plugin slant_rate \
--user-rate-plugin slant_rate \
--gs-rate-plugin-metadata ../../test/handover/slant_metadata_gs.json \
--user-rate-plugin-metadata ../../test/handover/slant_metadata_user.json \
--isl-loss-plugin pass_loss \
--gs-loss-plugin pass_loss \
--user-loss-plugin pass_loss \
--isl-rate 400 \
--gs-rate 200 \
--user-rate 50 \
--loss-isl 0.0 \
--loss-gs 0.0 \
--loss-user 0.0 \
--include-ground-stations \
--include-users \
--minimum-elevation 25 \
--duration 3600 \
--overwrite
```

Expected output:

- `generators/StarPerf_Simulator/data/XML_constellation/<constellation>_ext.h5`

## 2. Export epoch data

```bash
./nsb.py starperf-export \
--h5 data/XML_constellation/OneWeb_ext.h5 \
--sat-config-common ../../test/handover/sat-config-common.json
```

Expected output (for OneWeb):

- Epoch files under `examples/StarPerf/OneWeb/epochs`
- Satellite config file: `examples/StarPerf/OneWeb/sat-config.json`

## 3. Initialize emulation

Create a config for oracle routing from `sat-config.json` (for example, `sat-config-or-hops-ex.json`) and update `epoch-config.epoch-dir` to a new target directory where oracle routing plus expected-duration patching will write epochs, for example:

```json
"epoch-config": {
  "epoch-dir": "examples/StarPerf/OneWeb/epochs-or-hops-ex",
  "file-pattern": "NetSatBench-epoch*.json"
}
```

Initialize NSB so IPv6 addresses are assigned for oracle routing and `add-expected-duration.py`:

```bash
cd ../..
./nsb.py init -c examples/StarPerf/OneWeb/sat-config-or-hops-ex.json
```

## 4. Deploy nodes

```bash
./nsb.py deploy -t 8
```

## 5. Apply oracle routing (hop metric)

Create new epochs with hop-based oracle routing:

```bash
python3 utils/oracle-routing.py --out-epoch-dir examples/StarPerf/OneWeb/epochs-or-hops \
--epoch-dir examples/StarPerf/OneWeb/epochs \
--ip-version 6 \
--node-type-to-route satellite,gateway \
--node-type-to-install satellite \
--routing-metric hops \
--report \
--redundancy \ 
--drain-before-break-offset 2
```

### 5.1 Patch first two epoch timestamps

Open the first two epoch files and move their timestamps a few minutes earlier to allow initial sat-agent link setup and route injection to complete before traffic tests start. For example:

- `"time": "2023-09-30T23:56:00Z"`
- `"time": "2023-09-30T23:57:00Z"`

### 5.2 Add synthetic expected duration to link info

```bash
python3 utils/misc/add-expected-duration.py --epochs-dir examples/StarPerf/OneWeb/epochs-or-hops --output-dir examples/StarPerf/OneWeb/epochs-or-hops-ex
```

`examples/StarPerf/OneWeb/epochs-or-hops-ex` now contains epochs with oracle routing and expected duration for handover testing.

## 6. Schedule handover agents

Inject handover-agent startup tasks at emulation start:

```bash
./nsb.py run-inject -c examples/StarPerf/OneWeb/sat-config-or-hops-ex.json --offset-seconds 0 --node-type-list gateway --command-list '/app/grd/start-agent-grd.sh'
./nsb.py run-inject -c examples/StarPerf/OneWeb/sat-config-or-hops-ex.json --offset-seconds 0 --node-type-list user --command-list '/app/usr/start-agent-usr.sh'
```

## 7. Test plan

Inject ping/iperf commands at the desired time during the run.

### 7.1 Ping test

#### Upload files

```bash
./nsb.py cptype test/handover/usr user:/app
./nsb.py cptype test/handover/grd gateway:/app
./nsb.py cptype test/handover/ping_to_csv.sh user:/app
./nsb.py cptype test/handover/iperf3_to_csv.sh user:/app
```

#### Inject ping task

Use a `300s` offset so handover agents can finish route/link setup:

```bash
./nsb.py run-inject -c examples/StarPerf/OneWeb/sat-config-or-hops-ex.json \
--offset-seconds 300 \
--node-type-list user \
--command-list '/app/ping_to_csv.sh default 1200'
```

#### Run emulation

```bash
./nsb.py run
```

#### Recover ping results

Copy results from users to local `TARGET_DIR`:

```bash
./nsb.py cp usr7:/app/ping_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd5.csv $TARGET_DIR/ping_usr7_grd5.csv
./nsb.py cp usr6:/app/ping_grd3.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd3.csv $TARGET_DIR/ping_usr6_grd3.csv
./nsb.py cp usr5:/app/ping_grd3.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd3.csv $TARGET_DIR/ping_usr5_grd3.csv
./nsb.py cp usr4:/app/ping_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd5.csv $TARGET_DIR/ping_usr4_grd5.csv
./nsb.py cp usr3:/app/ping_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd5.csv $TARGET_DIR/ping_usr3_grd5.csv
./nsb.py cp usr2:/app/ping_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd5.csv $TARGET_DIR/ping_usr2_grd5.csv
./nsb.py cp usr1:/app/ping_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/ping_grd5.csv $TARGET_DIR/ping_usr1_grd5.csv
```

#### Reset links

```bash
./nsb.py reset
```

#### Restart user/gateway nodes for next test

```bash
./nsb.py node-restart --node usr1,usr2,usr3,usr4,usr5,usr6,usr7,grd1,grd2,grd3,grd4,grd5
```

### 7.2 iperf3 test

#### Configure TCP congestion control

Example (BBR):

```bash
./nsb.py exectype --node-type user 'sysctl -w net.ipv4.tcp_congestion_control=bbr'
./nsb.py exectype --node-type gateway 'sysctl -w net.ipv4.tcp_congestion_control=bbr'
```

#### Upload files

```bash
./nsb.py cptype test/handover/usr user:/app
./nsb.py cptype test/handover/grd gateway:/app
./nsb.py cptype test/handover/ping_to_csv.sh user:/app
./nsb.py cptype test/handover/iperf3_to_csv.sh user:/app
```

#### Inject iperf3 tasks

Manually update the epoch file modified by `run-inject` (for ping) with the following `run` section:

```json
"run": {
  "usr1": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5201 1200"
  ],
  "usr2": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5202 1200"
  ],
  "usr3": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5203 1200"
  ],
  "usr4": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5204 1200"
  ],
  "usr5": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5205 1200"
  ],
  "usr6": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5206 1200"
  ],
  "usr7": [
    "sleep 5 && /app/iperf3_to_csv.sh default 5207 1200"
  ],
  "grd1": [
    "iperf3 -s -p 5201 -D",
    "iperf3 -s -p 5202 -D",
    "iperf3 -s -p 5203 -D",
    "iperf3 -s -p 5204 -D",
    "iperf3 -s -p 5205 -D",
    "iperf3 -s -p 5206 -D",
    "iperf3 -s -p 5207 -D"
  ],
  "grd2": [
    "iperf3 -s -p 5201 -D",
    "iperf3 -s -p 5202 -D",
    "iperf3 -s -p 5203 -D",
    "iperf3 -s -p 5204 -D",
    "iperf3 -s -p 5205 -D",
    "iperf3 -s -p 5206 -D",
    "iperf3 -s -p 5207 -D"
  ],
  "grd3": [
    "iperf3 -s -p 5201 -D",
    "iperf3 -s -p 5202 -D",
    "iperf3 -s -p 5203 -D",
    "iperf3 -s -p 5204 -D",
    "iperf3 -s -p 5205 -D",
    "iperf3 -s -p 5206 -D",
    "iperf3 -s -p 5207 -D"
  ],
  "grd4": [
    "iperf3 -s -p 5201 -D",
    "iperf3 -s -p 5202 -D",
    "iperf3 -s -p 5203 -D",
    "iperf3 -s -p 5204 -D",
    "iperf3 -s -p 5205 -D",
    "iperf3 -s -p 5206 -D",
    "iperf3 -s -p 5207 -D"
  ],
  "grd5": [
    "iperf3 -s -p 5201 -D",
    "iperf3 -s -p 5202 -D",
    "iperf3 -s -p 5203 -D",
    "iperf3 -s -p 5204 -D",
    "iperf3 -s -p 5205 -D",
    "iperf3 -s -p 5206 -D",
    "iperf3 -s -p 5207 -D"
  ]
}
```

#### Run emulation

```bash
./nsb.py run
```

#### Recover iperf3 results

Copy results from users to local `TARGET_DIR`:

```bash
./nsb.py cp usr7:/app/iperf3_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd5.csv $TARGET_DIR/iperf3_usr7_grd5_bbr.csv
./nsb.py cp usr6:/app/iperf3_grd3.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd3.csv $TARGET_DIR/iperf3_usr6_grd3_bbr.csv
./nsb.py cp usr5:/app/iperf3_grd3.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd3.csv $TARGET_DIR/iperf3_usr5_grd3_bbr.csv
./nsb.py cp usr4:/app/iperf3_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd5.csv $TARGET_DIR/iperf3_usr4_grd5_bbr.csv
./nsb.py cp usr3:/app/iperf3_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd5.csv $TARGET_DIR/iperf3_usr3_grd5_bbr.csv
./nsb.py cp usr2:/app/iperf3_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd5.csv $TARGET_DIR/iperf3_usr2_grd5_bbr.csv
./nsb.py cp usr1:/app/iperf3_grd5.csv $TARGET_DIR/
mv $TARGET_DIR/iperf3_grd5.csv $TARGET_DIR/iperf3_usr1_grd5_bbr.csv
```

## 8. Visualize constellation in MATLAB

```bash
visualize_constellation_hdf5("../../config/XML_constellation/OneWeb.xml", ...
"../../data/XML_constellation/OneWeb_ext.h5", ...
"../../config/users/OneWeb.xml", ...
"../../config/ground_stations/OneWeb.xml", ...
"AddUserAccess", true, "CacheFile", "matlab_cache/OneWeb.mat");
```
