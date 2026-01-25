<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Configuration Reference Manual 

</div>

## Table of contents
1. [Worker Configuration File](#worker-configuration-file)
2. [Satellite Configuration File](#satellite-configuration-file)
3. [Epoch Configuration File](#epoch-configuration-file)

## Introduction
This document provides a unified reference for all JSON configuration files used by **NetSatBench**.  
It describes the structure, semantics, and constraints of:

- the **worker configuration file**, `worker-config.json`, which defines the execution cluster and networking substrate;  
- the **satellite configuration file**, `sat-config.json`, which defines the logical satellite system, routing behavior, and time evolution.
- the **epoch configuration files**, which define time-based events affecting the constellation emulation.

All configurations are expressed in **JSON format** and are consumed by the NetSatBench control scripts run on the control host.

---

## Worker Configuration File

### Overview 

The worker configuration file `worker-config.json` defines:

- The set of worker hosts available in the cluster  
- SSH connection parameters for remote management from the control host  
- The name of the Docker network bridge used to interconnect emulated containers on each worker  
- The IP subnet assigned to containers on each worker  
- The global supernet encompassing all container subnets across the cluster  

Each worker is uniquely identified by a logical name (e.g., `host-1`, `host-2`) and is associated with a set of required attributes.

---

### File Structure

```json  
{  
  "workers": {  
    "<worker-name>": {  
      "ip": "<management-ip>",  
      "ssh_user": "<ssh-username>",  
      "ssh_key": "<path-to-private-key>",  
      "sat-vnet": "<bridge-name>",  
      "sat-vnet-cidr": "<container-subnet>",  
      "sat-vnet-supernet": "<global-supernet>",
      "cpu": "<num-cpu-cores>",
      "mem": "<memory-available>" 
    },  
    ...  
  }  
}  
```

---

### Fields Description

#### `workers`

Top-level object containing the definition of all worker hosts in the cluster.

Each key under `workers` represents a logical worker identifier, which is used throughout the system to assign emulated nodes to specific hosts.

---

#### Per-Worker Fields

Each worker entry must define the following fields:

##### `ip`
- Type: string  
- Description: Management IP address of the worker host, reachable from the control host.  
- Usage: Used by control scripts to establish SSH connections and as the bind address for VXLAN tunnels.

##### `ssh_user`
- Type: string  
- Description: Username used by the control host to connect to the worker via SSH.  
- Requirements: This user must have passwordless `sudo` access and permissions to run Docker commands.

##### `ssh_key`
- Type: string  
- Description: Absolute path to the private SSH key used by the control host for authentication.  
- Notes: The key must be readable by the control scripts and authorized on the worker host.

##### `sat-vnet`
- Type: string  
- Description: Name of the Docker network (Linux bridge) created on the worker host to interconnect containers of the emulated nodes.  
- Usage: All containers deployed on the worker are attached to this bridge.

##### `sat-vnet-cidr`
- Type: string (CIDR notation)  
- Description: Unique IP subnet assigned to eth0 interfaces of containers running on this worker.  
- Constraints:  
  - Must be unique per worker  
  - Must be a subnet of `sat-vnet-supernet`  
- Example: `172.100.0.0/16`

##### `sat-vnet-supernet`
- Type: string (CIDR notation)  
- Description: Global supernet encompassing all container subnets across the cluster.  
- Usage: Ensures routable underlay IP addressing between containers on different workers without NAT.  
- Important: The underlying physical or virtual Ethernet network must allow unrestricted IP connectivity within this supernet.

##### `cpu`
- Type: string  
- Description: Number of CPU cores available for container execution on the worker host.  
- Usage: Used by the control scripts to schedule emulated nodes based on resource availability.

##### `mem`
- Type: string  
- Description: Amount of memory available for container execution on the worker host.  
- Usage: Used by the control scripts to schedule emulated nodes based on resource availability.

---
### Example

```json  
{
"workers": {
    "host-1": {
      "ip": "10.0.1.215",
      "ssh_user": "ubuntu",
      "ssh_key": "/home/ubuntu/.ssh/id_rsa",
      "sat-vnet": "sat-vnet",
      "sat-vnet-cidr":"172.100.0.0/16",
      "sat-vnet-supernet": "172.0.0.0/8",
      "cpu": "4",
      "mem": "6GiB"
    },
    "host-2": {
      "ip": "10.0.1.144",
      "ssh_user": "ubuntu",
      "ssh_key": "/home/ubuntu/.ssh/id_rsa",
      "sat-vnet": "sat-vnet",
      "sat-vnet-cidr":"172.101.0.0/16",
      "sat-vnet-supernet": "172.0.0.0/8",
      "cpu": "4",
      "mem": "6GiB"
    }
  }
}
```

## Satellite Configuration File

### Overview

The satellite configuration file, `sat-config.json`, defines:

- Global Layer-3 and routing options of the VXLAN-based overlay network that are shared by all nodes  
- The set of satellite nodes in the constellation  
- The set of ground stations and users connected to the constellation  
- Placement of each node on worker hosts  
- Container images, requirements, and per-node internal overlay subnets
- Time evolution parameters for the constellation emulation 

Each emulated node (satellite, ground station, or user) is uniquely identified by a logical name (e.g., `sat1`, `grd1`, `usr1`) that should use less than 8 characters.

---

### File Structure

```json  
{  
  "L3-config-common": { ... },  
  "epoch-config": { ... },  
  "satellites": {  
    "<satellite-name>": { ... },  
    ...  
  },  
  "grounds": {  
    "<ground-name>": { ... },  
    ...  
  },
  "users": {  
    "<user-name>": { ... },  
    ...  
  }
}  
```

---

### Fields Description

#### `L3-config-common`

Defines Layer-3 and routing parameters of the VXLAN-based overlay network that are globally applied to all emulated nodes.

##### `enable-netem`
- Type: boolean  
- Description: Enables or disables traffic control (tc netem) for emulating delay, loss, and bandwidth.  
- Usage: When `true`, link parameters defined in epoch files are enforced at run time.

##### `auto-assign-ips`
- Type: boolean  
- Description: Enables or disables automatic assignment of internal IP subnets to nodes.
- Usage: If `true`, nodes without an explicit `auto-assign-ips = false` field are assigned subnets from the block specified in `auto-assign-cidr` in the order they are defined in the configuration file.

##### `auto-assign-cidr`
- Type: string (CIDR notation)  
- Description: Base CIDR block for overlay addressing from which sequential /30 subnets are automatically assigned to nodes when `auto-assign-ips` is `true`.
  
##### `enable-routing`
- Type: boolean  
- Description: Enables or disables overlay IP routing inside emulated nodes.

##### `routing-module`
- Type: string (needed if `enable-routing` is `true`)
- Description: Identifier of the routing configuration module used by the `sat-agent` (see [routing-interface](routing-interface.md)).  
- Example: `extra.isis`

##### `isis-area-id`
- Type: string  (needed if `routing-module` is `extra.isis`)
- Description: IS-IS area identifier applied to all nodes when IS-IS routing is enabled.

---

#### `epoch-config`

Defines how the time evolution of the constellation is driven.

##### `epoch-dir`
- Type: string  
- Description: Path to the directory containing epoch definition files.

##### `file-pattern`
- Type: string  
- Description: Filename pattern used to select epoch files within `epoch-dir`. Epoch files matching this pattern are loaded and applied in **lexicographical order**.

---

#### `satellites`

Defines the set of satellite nodes in the emulated constellation.
Each satellite entry, has a key that represents the logical satellite name.
The name must be unique across all nodes (satellites, ground stations, and users) and shold have a length <u>lower than 8 characters</u>.
The value contains the following fields:

##### `worker`
- Type: string  (optional)
- Description: Worker host on which the satellite container is deployed. If omitted, the `constellation-init` script automatically assigns the node to a worker based on resource availability.

##### `image`
- Type: string  
- Description: Docker image used to instantiate the satellite container.

##### `cpu-request`
- Type: string (optional)
- Description: CPU resources requested for the satellite container (e.g., `100m` for 0.1 CPU core). Used to control the scheduling of nodes on workers and as a priority level in case of CPU resource contention (e.g., see docker --cpu-shares).

##### `mem-request`
- Type: string (optional)
- Description: Memory resources requested for the satellite container (e.g., `200Mi`). Used to control the scheduling of nodes on workers and as a priority OOM killing level in case of memory exhaustion (e.g., see docker --memory-reservation).

##### `cpu-limit`
- Type: string (optional)
- Description: CPU resources limit for the satellite container (e.g., `200m` for 0.2 CPU core). Used to cap the maximum CPU usage of the container (similar to Kubernetes CPU limits).

##### `mem-limit`
- Type: string (optional)
- Description: Memory resources limit for the satellite container (e.g., `400Mi`). Used to cap the maximum memory usage of the container (similar to Kubernetes memory limits).

##### `subnet_cidr`
- Type: string (CIDR notation, optional)  
- Description: Internal IP subnet assigned to the satellite node. The subnet must be at least a `/30`. The last usable IP address within this subnet is assigned to the node’s loopback interface and is used for routing over the L2/VXLAN network fabric. If this field is omitted, no internal subnet is assigned to the node.

##### `L3-config`
- Type: object (optional)  
- Description: Per-node Layer-3 configuration overrides.  
- Fields: use the same fields and semantics as `L3-config-common`.

---

#### `grounds`
Defines the set of ground station nodes connected to the constellation.  
Ground stations use the same fields and semantics as satellites.

---

#### `users`
Defines the set of user nodes connected to the constellation.  
Users use the same fields and semantics as satellites.

---
### Example

```json  
{
  "L3-config-common": {
    "enable-netem"  : true,
    "enable-routing" : true,
    "routing-module": "extra.isis",
    "isis-area-id": "0001",
    "auto-assign-ips": true,
    "auto-assign-cidr": "192.168.0.0/16"
  },
  "epoch-config": {
    "epoch-dir": "examples/10nodes/constellation-epochs",
    "file-pattern": "NetSatBench-epoch*.json"
  },
  "satellites": {
    "sat1": {
      "image": "msvcbench/sat-container:latest",
      "cpu-request": "100m",
      "mem-request": "200Mi",
      "cpu-limit": "200m",
      "mem-limit": "400Mi"
    }
  },
  "grounds": {
    "grd1": {
      "image": "msvcbench/sat-container:latest",
      "cpu-request": "100m",
      "mem-request": "200Mi",
      "cpu-limit": "200m",
      "mem-limit": "400Mi"
    }
  },
  "users": {
    "usr1": {
      "image": "msvcbench/sat-container:latest",
      "cpu-request": "100m",
      "mem-request": "200Mi",
      "cpu-limit": "200m",
      "mem-limit": "400Mi",
      "subnet_cidr": "172.99.0.0/30",
      "L3-config": {
        "enable-netem": false,
        "auto-assign-ips": false
      }
    }
  }
}
```
---
## Epoch Configuration File 

### Overview

An epoch configuration file defines:

- The relative emulation time at which the epoch is applied  
- Links to be added between emulated nodes  
- Existing links whose parameters must be updated  
- Links to be removed between emulated nodes   
- Commands to be executed inside specific emulated nodes  

Epoch files are loaded sequentially by the control logic (`constellation-run`) and applied at the specified emulation time with an offset equal to the difference between the current epoch time and the first epoch time.

The epoch file names are expected to terminate with a numerical suffix that indicates their processing sequence.  E.g., `NetSatBench-epoch0.json`, `NetSatBench-epoch1.json`, etc.  
Each application of an epoch file modifies the link and command (run) state stored in Etcd and `sat-agent` of emulated nodes are promptly notified. 

The first epoch file should contain all initial link definitions required to establish connectivity between nodes at simulation start time.

---

### File Structure

```json  
{  
  "time": "<ISO-8601 timestamp>",  

  "links-add": [ ... ],  
  "links-update": [ ... ],  
  "links-del": [ ... ],  

  "run": { ... }  
}  
```

All fields except `time` are optional.  
Empty arrays or empty objects indicate that no action of that type is performed during the epoch.

---

### Fields Description

#### `time`

- Type: string (ISO-8601 timestamp)  
- Description: Absolute simulation time at which the epoch is applied.  
- Example: `2024-06-01T12:02:00Z`

---

#### `links-add`

Defines a list of **new links** to be created between pairs of emulated nodes at the epoch time.

Each entry describes a <u>bidirectional</u> Layer-2 overlay link implemented via a VXLAN tunnel between two emulated nodes.

##### Fields

- `endpoint1`  
  - Type: string  
  - Description: Logical name of the first endpoint (e.g., `sat1`, `grd1`).

- `endpoint2`  
  - Type: string  
  - Description: Logical name of the second endpoint.

- `rate`  
  - Type: string  
  - Description: Link bandwidth, expressed using Linux tc syntax (e.g., `10mbit`).

- `loss`  
  - Type: number  
  - Description: Packet loss probability (percentage).

- `delay`  
  - Type: string  
  - Description: One-way link delay (e.g., `5ms`).

---

#### `links-update`

Defines updates to the characteristics of **existing links**.

Only the parameters specified in an update entry are modified; unspecified parameters retain their previous values.

##### Fields

The fields are identical to those used in `links-add`:
- `endpoint1`  
- `endpoint2`  
- `rate`  
- `loss`  
- `delay`

---

#### `links-del`

Defines links that must be **removed** from the system at the epoch time.

Removal tears down the corresponding overlay VXLAN tunnels and associated traffic control configuration.

##### Fields

- `endpoint1`  
  - Type: string  
  - Description: Logical name of the first endpoint.

- `endpoint2`  
  - Type: string  
  - Description: Logical name of the second endpoint.

Optional fields (e.g., `rate`, `loss`, `delay`) may be present but are ignored during deletion.

---

#### `run`

Defines **commands to be executed inside emulated nodes** at the epoch time.

Commands are executed sequentially, inside the container associated with the specified node, using the shell environment provided by the container image.

###### Structure

```json  
"run": {  
  "<node-name>": [  
    "<command-1>",  
    "<command-2>",  
    ...  
  ]  
}  
```

Each key represents the logical name of an emulated node, and its value is an ordered list of shell commands to execute. Long-running applications should be launched in background or detached sessions (e.g., using `screen` or `tmux`).

---

### Example

```json  
{  
  "time": "2024-06-01T12:02:00Z",  

  "links-add": [  
    {  
      "endpoint1": "usr1",  
      "endpoint2": "sat2",  
      "rate": "50mbit",  
      "loss": 0,  
      "delay": "5ms"  
    }  
  ],  

  "links-update": [  
    {  
      "endpoint1": "grd1",  
      "endpoint2": "usr1",  
      "rate": "20mbit",  
      "delay": "10ms"  
    }  
  ],  

  "links-del": [  
    {  
      "endpoint1": "usr1",  
      "endpoint2": "sat1"  
    }  
  ],  

  "run": {  
    "grd1": [  
      "sleep 10",  
      "screen -dmS iperf_test iperf3 -s"  
    ]  
  }  
}  
```

---

## Cross-File Consistency Requirements

- Worker names referenced in `sat-config.json` must exist in the worker configuration file `worker-config.json`.  
- All endpoint names in an epoch file must correspond to valid nodes defined in `sat-config.json`. 

