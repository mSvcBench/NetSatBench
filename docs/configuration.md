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
- the **satellite configuration file**, `sat-config.json`, which defines static configuration paramenters of the satellite system.
- the **epoch configuration files**, which define time-based events affecting the dynamic behavior of the satellite system.

All configurations are expressed in **JSON format** and are processed by the control scripts run on the control host and inserted in the **Etcd** datastore.

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
      "ssh-user": "<ssh-username>",  
      "ssh-key": "<path-to-private-key>",  
      "sat-vnet": "<bridge-name>",  
      "sat-vnet-cidr": "<container-subnet>",  
      "sat-vnet-supernet": "<containers-supernet>",
      "cpu": "<num-cpu-cores>",
      "mem": "<memory-available>" 
    },  
    ...  
  }  
}  
```

---
### Example

```json  
{
"workers": {
    "host-1": {
      "ip": "10.0.1.215",
      "ssh-user": "ubuntu",
      "ssh-key": "/home/ubuntu/.ssh/id_rsa",
      "sat-vnet": "sat-vnet",
      "sat-vnet-cidr":"172.100.0.0/16",
      "sat-vnet-supernet": "172.0.0.0/8",
      "cpu": "4",
      "mem": "6GiB"
    },
    "host-2": {
      "ip": "10.0.1.144",
      "ssh-user": "ubuntu",
      "ssh-key": "/home/ubuntu/.ssh/id_rsa",
      "sat-vnet": "sat-vnet",
      "sat-vnet-cidr":"172.101.0.0/16",
      "sat-vnet-supernet": "172.0.0.0/8",
      "cpu": "4",
      "mem": "6GiB"
    }
  }
}
```
---

### Fields Description

#### `workers`

Top-level object containing the definition of all worker hosts in the cluster. Each key under `workers` represents a logical worker identifier.

---

#### Per-Worker Fields

##### `ip`
- Type: string  
- Description: Management IP address of the worker host, reachable from the control host.  
- Usage: Used by control scripts to establish SSH connections and as the bind address for VXLAN tunnels.

##### `ssh-user`
- Type: string  
- Description: Username used by the control host to connect to the worker via SSH.  
- Requirements: This user must have passwordless `sudo` access and permissions to run Docker commands.

##### `ssh-key`
- Type: string  
- Description: Absolute path to the private SSH key used by the control host for authentication.  
- Notes: The key must be readable by the control scripts and authorized on the worker host.

##### `sat-vnet`
- Type: string  
- Description: Name of the Docker network (Linux bridge) created on the worker host to interconnect containers of the emulated nodes.  
- Usage: All containers deployed on the worker are attached to this bridge.

##### `sat-vnet-cidr`
- Type: string (CIDR notation)  
- Description: Unique underlay IP subnet assigned to eth0 interfaces of containers running on this worker.  
- Constraints:  
  - Must be unique per worker  
  - Must be a subnet of `sat-vnet-supernet`  
- Example: `172.100.0.0/16`

##### `sat-vnet-supernet`
- Type: string (CIDR notation)  
- Description: Underlay CIDR supernet encompassing all container subnets (sat-vnet-cidr) across the cluster.  
- Usage: Ensures routable underlay IP addressing between containers on different workers without NAT.  
- Important: The underlying physical or virtual Ethernet network must allow unrestricted IP connectivity within this supernet.

##### `cpu`
- Type: string  
- Description: Number of CPU cores available for container execution on the worker host. Allowed units: integer or fractional (e.g., `4`, `2.5`) of CPU cores, or `m` (millicores).  
- Usage: Used by the control scripts to schedule emulated nodes based on resource availability.

##### `mem`
- Type: string  
- Description: Amount of memory available for container execution on the worker host. Allowed units: `KiB`, `MiB`, `GiB`, `TiB`. 
- Usage: Used by the control scripts to schedule emulated nodes based on resource availability.

---

## Satellite Configuration File

### Overview

The satellite configuration file, `sat-config.json`, defines:

- Common config data shared by all nodes  
- The set of nodes in the constellation with specific values overriding global parameters  
- Directory and filename pattern for epoch files that drive time evolution  


Each emulated node (satellite, ground station, or user) is uniquely identified by a logical name (e.g., `sat1`, `grd1`, `usr1`) that should use less than 8 characters.

---

### File Structure

```json  
{  
  "node-config-common": { ... },  
  "epoch-config": { ... },  
  "nodes": {  
    "<node-name>": { ... },  
    ...
  ...
}  
```

---

### Example

```json  
{
  "node-config-common": {
    "type": "undefined",
    "n_antennas": 2,
    "metadata": {},  
    "image": "msvcbench/sat-container:latest",
    "sidecars": [],
    "cpu-request": "100m",
    "mem-request": "200MiB",
    "cpu-limit": "200m",
    "mem-limit": "400MiB",
    "L3-config": {
      "enable-netem"  : true,
      "enable-routing" : true,
      "routing-module": "extra.isis",
      "routing-metadata": {
        "isis-area-id": "0001"
      },
      "auto-assign-ips": true,
      "auto-assign-super-cidr": [
          {"matchType":"satellite","super-cidr":"192.168.0.0/16"},
          {"matchType":"gateway","super-cidr":"172.10.0.0/16"},
          {"matchType":"user","super-cidr":"172.11.0.0/16"}
      ]
    }
  },
  "epoch-config": {
    "epoch-dir": "examples/10nodes/constellation-epochs",
    "file-pattern": "NetSatBench-epoch*.json"
  },
  "nodes": {
    "sat1": {
      "type": "satellite",
      "n_antennas": 5,
      "metadata": {
        "orbit": {
          "TLE": [
            "1 47284U 20100AC  25348.63401725  .00000369  00000+0  98748-3 0  9994",
            "2 47284  87.8941  27.3203 0001684  91.6865 268.4456 13.12589579240444"
          ]
          },
        "labels": {
          "name": "ONEWEB-0138"
        }
      }
    },
    "sat2": {
      "type": "satellite",
      "n_antennas": 5
    },
    ...,
    "grd1": {
      "type": "gateway",
      "n_antennas": 2,
      "cpu-request": "200m",
      "mem-request": "400MiB",
      "cpu-limit": "400m",
      "mem-limit": "800MiB",
      "metadata": {
        "location": {
          "latitude": 37.4275,
          "longitude": -122.1697,
          "altitude": 30
        },
        "labels": {
          "name": "stanford_ground_station"
        }
      }
    },
    "usr1": {
      "type": "user",
      "n_antennas": 2,
      "metadata": {
        "location": {
          "latitude": 37.7749,
          "longitude": -122.4194,
          "altitude": 20
        },
        "labels": {
          "name": "san_francisco_user"
        }
      },
      "L3-config": {
        "enable-netem": false,
        "auto-assign-ips": false,
        "cidr": "172.99.0.0/30"
      }
    }
  }
}
```
### Fields Description

#### `node-config-common`

Defines common  of the VXLAN-based overlay network that are globally applied to all emulated nodes.

#### `node-config-common` Fields
##### `type`
- Type: string  
- Description: Type of the node. Recommended values: `satellite`, `gateway`, `user`. Custom strings are possible.  
- Usage: Used for classification, automatic IP assignment, icons, etc.

##### `n_antennas`
- Type: integer  
- Description: Number of antennas of the node.  
- Usage: Not used by control scripts

##### `metadata`
- Type: object  
- Description: Arbitrary key-value pairs associated with the node.  
- Usage: Not used by control scripts

##### `image`
- Type: string  
- Description: Docker image of sat-agent used to instantiate the container for the node.
- Usage: Must be a valid image accessible from the worker hosts.

##### `sidecars`
- Type: array of strings  
- Description: List of Docker images of sidecar containers to be deployed alongside the main node container.
- Usage: Not yet supported by control scripts.

##### `cpu-request`
- Type: string (optional)  
- Description: CPU resources requested for the node container (e.g., `100m` for 0.1 CPU core). 
- Usage: Used to control the scheduling of nodes on workers and as a priority level in case of CPU resource contention (e.g., see docker --cpu-shares).

##### `mem-request`
- Type: string (optional)  
- Description: Memory resources requested for the node container (e.g., `200MiB`). 
- Usage: Used to control the scheduling of nodes on workers and as a priority OOM killing level in case of memory exhaustion (e.g., see docker --memory-reservation).

##### `cpu-limit`
- Type: string (optional)  
- Description: CPU resources limit for the node container (e.g., `200m` for 0.2 CPU core). 
- Usage: Used to cap the maximum CPU usage of the container (e.g., see docker --cpus).

##### `mem-limit`
- Type: string (optional)  
- Description: Memory resources limit for the node container (e.g., `400MiB`). 
- Usage: Used to cap the maximum memory usage of the container (e.g., see docker --memory).

##### `L3-config`
- Type: object  
- Description: Layer-3 network configuration parameters applied to satellite links (VXLAN tunnels).
- Fields: see below.

###### `enable-netem`
- Type: boolean  
- Description: Enables or disables traffic control (tc netem) for emulating delay, loss, and bandwidth.  
- Usage: When `true`, satelite link parameters defined in epoch files are enforced at run time.

###### `enable-routing`
- Type: boolean
- Description: Enables or disables IP routing over satellite links.
- Usage: If `true`, the routing module specified in `routing-module` is activated.

###### `routing-module`
- Type: string (needed if `enable-routing` is `true`)
- Description: Identifier of the routing configuration module used by the `sat-agent` (see [routing-interface](routing-interface.md)) for the satellite links.  
- Example: `extra.isis`

###### `auto-assign-ips`
- Type: boolean  
- Description: Enables or disables automatic assignment of internal IP subnets routed over satellite links.
- Usage: If `true`, nodes are assigned subnets from the block specified in `auto-assign-cidr`

###### `auto-assign-super-cidr`
- Type: JSON array of objects  
- Description: List of rules for automatic assignment of internal IP subnets to nodes based on their type.
- Each rule object contains the following fields::
  - `matchType`: string  
    - Description: Node type to match (e.g., `satellite`, `gateway`, `user`).
  - `super-cidr`: string (CIDR notation)  
    - Description: Base CIDR block for overlay addressing from which sequential /30 subnets are automatically assigned to nodes of the specified type. Use a subnet different from those used in the underlay network, e.g, `sat-vnet-supernet` in worker configuration and the network used by physical interfaces of hosts (e.g., eth0).
---

#### `epoch-config`

Defines how the time evolution of the constellation is driven.

#### `epoch-config` Fields

##### `epoch-dir`
- Type: string  
- Description: Path to the directory containing epoch definition files.
- Usage: Epoch files in this directory matching the `file-pattern` are loaded and applied sequentially during constellation run time.

##### `file-pattern`
- Type: string  
- Description: Filename pattern used to select epoch files within `epoch-dir`. Epoch files matching this pattern are loaded and applied in **lexicographical order**.

---

### `nodes`

Defines the set of nodes in the emulated constellation.
Each node entry has a key that represents the logical `node-name`.
The name must be unique across all nodes and should have a length <u>lower than 8 characters</u>.

### Per-Node Fields 
The fields of a node can contain any field of `node-config-common` to override the corresponding common value.
Besides the ovverrided common fields, each node can also define the following additional fields to avoid automatic assignment:

#### `worker`
- Type: string  (optional)
- Description: Worker host on which the satellite container is deployed. If omitted, the `constellation-init` script automatically assigns the node to a worker based on resource availability.
- Usage: Must correspond to a valid worker name defined in the worker configuration file `worker-config.json`.

#### `cidr`
- Type: string (CIDR notation)  (optional)
- Description: Specific IP subnet assigned to a node and routed over satellite links. If omitted, the subnet is automatically assigned if `auto-assign-ips` is `true`. Otherwise the node will not have an IP address.
- Usage: Must be a /30 subnet out of the block defined in `auto-assign-super-cidr` to avoid overlaps.


---
## Epoch Configuration File 

### Overview

An epoch configuration file defines dynamic events that modify the constellation state at specific emulation times.
Each epoch file can specify:

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
      "screen -dmS iperf_test iperf3 -s"  
    ]  
  }  
}  
```
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

#### `links-add` Fields

##### `endpoint1`  
  - Type: string  
  - Description: Logical name of the first endpoint (e.g., `sat1`, `grd1`).

##### `endpoint2`  
  - Type: string  
  - Description: Logical name of the second endpoint.

##### `rate`  
  - Type: string  
  - Description: Link bandwidth, expressed using Linux tc syntax (e.g., `10mbit`).

##### `loss`  
  - Type: number  
  - Description: Packet loss probability (percentage).

##### `delay`  
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



---

## Cross-File Consistency Requirements

- Worker names referenced in `sat-config.json` must exist in the worker configuration file `worker-config.json`.  
- All endpoint names in an epoch file must correspond to valid nodes defined in `sat-config.json`. 

