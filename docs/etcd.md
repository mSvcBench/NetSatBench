<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench ETCD Keyspace 

</div>

The ETCD keyspace used by NetSatBench follows a hierarchical, declarative schema, where each prefix corresponds to a logical subsystem of the emulator. 
The content of the Etcd keyspace can be inspected using standard Etcd tools, e.g., from the control host via the command:

```bash
etcdctl get --prefix /config
```

The main prefixes are structured as follows:
```
/config
│
├── workers
│   ├── <worker-name>
│   ├── ...
│   └── <worker-name>
│
├── epoch-config
│
├── etchosts
│   ├── <node-name>
│   ├── ...
│   └── <node-name>
│
├── nodes
│   ├── <node-name>
│   ├── ...
│   └── <node-name>
│
├── links
│   ├── <node-name>/<interface-name>
│   ├── ...
│   └── <node-name>/<interface-name>
│
└── run
    ├── <node-name>
    ├── ...
    └── <node-name>
```
---

## /config/workers/

This prefix contains the list of worker hosts participating in the emulation, indexed by worker name.  
Each worker is associated with a JSON configuration derived from the `worker-config.json` file (see the [configuration manual](configuration.md)).  
These entries are automatically created and managed by the `control/system-init-docker.py` script.

### Example

`/config/workers/host-1  `

```json
{
  "ip": "10.0.1.215",
  "ssh-user": "ubuntu",
  "ssh-key": "/home/ubuntu/.ssh/id_rsa",
  "sat-vnet": "sat-vnet",
  "sat-vnet-cidr": "172.20.0.0/24",
  "sat-vnet-super-cidr": "172.20.0.0/16",
  "cpu": "4",
  "mem": "6GiB"
}
```

---
## /config/epoch-config|nodes
These prefixes contain the configuration data of the emulated node indexed by name as defined in the `sat-config.json` file (see the [configuration manual](configuration.md)). They are automatically created and managed by the `control/nsb-init.py` script.

### Example

`/config/nodes/sat1`

```json
{
  "type": "satellite", 
  "n_antennas": 5, 
  "metadata": {
    "orbit": {
      "TLE": [
        "1 47284U 20100AC  25348.63401725  .00000369  00000+0  98748-3 0  9994", 
        "2 47284  87.8941  27.3203 0001684  91.6865 268.4456 13.12589579240444"
        ]
    }, 
    "labels": {"name": "ONEWEB-0138"}}, 
    "image": "msvcbench/sat-container:latest", 
    "sidecars": [], 
    "cpu-request": 0.1, 
    "mem-request": "200MiB", 
    "cpu-limit": 0.2, 
    "mem-limit": "400MiB", 
    "L3-config": {"enable-netem": true, "enable-routing": true, "routing-module": "extra.routing.isis", "routing-metadata": {"isis-area-id": "0001"}, "auto-assign-ips": true, "auto-assign-super-cidr": [{"matchType": "satellite", "super-cidr": "172.100.0.0/16"}, {"matchType": "gateway", "super-cidr": "172.101.0.0/16"}, {"matchType": "user", "super-cidr": "172.102.0.0/16"}], "cidr": "172.100.0.0/16"}, 
    "worker": "host-2", 
    "eth0_ip": "172.20.0.2"
}

```
In addition to the parameters specified in `sat-config.json`, the `eth0_ip` field defines the IP address assigned to the eth0 interface of the node container. This address is used to establish the overlay VXLAN links between nodes and is automatically managed by the sat-agent running inside each container during initialization.
The `L3-config:cidr` and `worker`fields can be automatically assigned if not set in the `sat-config.json` file.


`/config/epoch-config`

```json
{
 "epoch-dir": "examples/10nodes/epochs", 
 "file-pattern": "NetSatBench-epoch*.json"
}
```

## /config/etchosts/
This prefix holds the overlay IP address assigned to the loopback interface (if assigned) for each emulated node, indexed by node name.
These entries are automatically created and managed by the `sat-agent`, which configures the `/etc/hosts` file inside each container, allowing workloads to refer to nodes by name rather than by overlay IP address.

### Example

`/config/etchosts/sat1`

```json
 172.100.0.1
```

## /config/links/
This prefix contains the state of all satellite links in the current epoch, indexed by source node and interface name.
Each entry stores a JSON object describing the current link parameters, such as delay, packet loss, bandwidth, and VXLAN identifier.
These entries are automatically created and managed by the `control/nsb-run.py` script while processing epoch files.

### Example

`/config/links/sat1/vl_sat2_1`
  
```json
{
 "endpoint1": "sat1", 
 "endpoint2": "sat2", 
 "rate": "10mbit",
 "loss": 0, 
 "delay": "1ms", 
 "vni": 13475210
}
```
The key suffix `sat1/vl_sat2_1` indicates that the VXLAN interface name on sat1 for this link should be `vl_sat2_1`. The VNI value is unique and automatically assigned by the `control/nsb-run.py` script when the link is created.

## /config/run/
This prefix stores runtime information for each emulated node, indexed by node name.
Each entry contains a JSON array specifying the sequence of commands to be executed during the current epoch.
These entries are automatically created and managed by the `control/nsb-run.py` script.

### Example

`/config/run/grd1`

```json
["sleep 10", "screen -dmS iperf_test iperf3 -s"]
```
The key `grd1` indicates that these commands are to be executed inside the container of node `grd1` during the current epoch.