<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Configuration Reference Manual

</div>

---

## Table of Contents
1. [Worker Configuration File](#worker-configuration-file)
2. [Satellite Configuration File](#satellite-configuration-file)
3. [Epoch Configuration File](#epoch-configuration-file)
4. [Cross-File Consistency Requirements](#cross-file-consistency-requirements)

---

## Introduction

This document provides a unified reference for all JSON configuration files used by **NetSatBench**. It describes the structure, semantics, and constraints of:

- the **worker configuration file**, `worker-config.json`, defining the execution cluster and underlay networking substrate;
- the **satellite configuration file**, `sat-config.json`, defining static configuration parameters of the satellite system;
- the **epoch configuration files**, defining time-based events affecting dynamic behavior at runtime.

All configurations are expressed in **JSON format** and are processed by control scripts executed on the control host. Configuration data are stored in the **Etcd** datastore and consumed by runtime components.

NetSatBench distinguishes between:
- an **underlay network**, providing IP connectivity between containers across workers; and
- an **overlay network**, implemented via VXLAN tunnels, representing satellite links whose characteristics evolve over time.

---

## Worker Configuration File

### Overview

The worker configuration file, `worker-config.json`, defines the set of worker hosts forming the execution cluster, their management access parameters, the underlay container networking configuration, and the compute resources available for scheduling emulated nodes.

---

### File Structure

```json
{
  "workers-common": {
    "ssh-user": "<ssh-username>",
    "ssh-key": "<path-to-private-key>",
    "sat-vnet": "<bridge-name>",
    "sat-vnet-super-cidr": "<containers-supernet>",
    "cpu": "<num-cpu-cores>",
    "mem": "<memory-available>"
  }
  "workers": {
    "<worker-name>": {
      "ip": "<management-ip>",
      "ssh-user": "<ssh-username>",
      "ssh-key": "<path-to-private-key>",
      "sat-vnet": "<bridge-name>",
      "sat-vnet-cidr": "<container-subnet>",
      "cpu": "<num-cpu-cores>",
      "mem": "<memory-available>"
    }
  }
}
```
---
### Example
```json  
{
  "workers-common": {
      "ssh-user": "ubuntu",
      "ssh-key": "/home/ubuntu/.ssh/id_rsa",
      "sat-vnet": "sat-vnet",
      "sat-vnet-super-cidr": "172.20.0.0/16"
  },
  "workers": {
      "host-1": {
        "ip": "10.0.1.215",
        "sat-vnet-cidr":"172.20.0.0/24",
        "cpu": "2",
        "mem": "2GiB"
      },
      "host-2": {
        "ip": "10.0.1.144",
        "sat-vnet-cidr":"172.20.1.0/24",
        "cpu": "2",
        "mem": "2GiB"
      },
  }
}
```
---

### Field Descriptions

#### `workers-common`
* **Type**: object (mandatory)
* **Description**: Common configuration applied to all workers unless explicitly overridden in a specific entry under workers. Each field has the same semantics as the corresponding per-worker field described below. In addition, workers-common must define the `sat-vnet-super-cidr` field, whose value is a CIDR string representing the supernet encompassing all worker `sat-vnet-cidr` subnets. This field is global, cannot be overridden on a per-worker basis, and must not overlap with any underlay network (e.g., physical interfaces of worker hosts). This requirement ensures direct IP routing between containers across workers without the use of NAT.


#### `workers`

* **Type**: object 
* **Requirement**: mandatory
* **Description**: Top-level object containing all worker host definitions. Each key is a unique logical worker identifier (e.g., `host-1`).

---

#### Per-Worker Fields

##### `ip`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Management IP address of the worker host, reachable from the control host. Used for worker management via SSH.

##### `ssh-user`

* **Type**: string 
* **Requirement**: mandatory, if not included in `workers-common`
* **Description**: SSH username used by the control host. The SSH user must have passwordless `sudo` privileges and permission to execute Docker commands.

##### `ssh-key`

* **Type**: string 
* **Requirement**: mandatory, if not included in `workers-common`
* **Description**: Absolute path to the private SSH key used for authentication. The key must be readable by the control scripts and authorized on the worker host.

##### `sat-vnet`

* **Type**: string 
* **Requirement**: mandatory, if not included in `workers-common`
* **Description**: Name of the Docker network (Linux bridge) created on the worker host to interconnect all containers deployed on that worker.

##### `sat-vnet-cidr`

* **Type**: string (CIDR notation)
* **Requirement**: mandatory, if not included in `workers-common`
* **Description**: Underlay IP subnet assigned eth0 interfaces of containers deployed on the worker and routed by the Docker bridge. Must be unique per worker and must be a subnet of `sat-vnet-super-cidr`. Its size must accommodate all containers deployed on the worker.


##### `cpu`

* **Type**: string 
* **Requirement**: mandatory, if not included in `workers-common`
* **Units / Format**: n. of CPU cores or millicore (e.g., `4`, `2.5`, `500m`).
* **Description**: CPU capacity available on the worker for container execution, used by control scripts for container scheduling.

##### `mem`

* **Type**: string 
* **Requirement**: mandatory, if not included in `workers-common`
* **Units / Format**: Binary units `KiB`, `MiB`, `GiB`, `TiB` (e.g., `6GiB`).
* **Description**: Memory capacity available on the worker for container execution, used by control scripts for container scheduling.

---

## Satellite Configuration File

### Overview

The satellite configuration file, `sat-config.json`, defines:

* common configuration parameters applied to all nodes;
* per-node overrides;
* the configuration of epoch files that drive the temporal evolution of the constellation.

Each node is identified by a unique logical name (e.g., `sat1`, `grd1`, `usr1`) that must be **shorter than 8 characters**.

---

### File Structure

```json
{
  "node-config-common": { ... },
  "epoch-config": { ... },
  "nodes": {
    "<node-name>": { ... }
  }
}
```
---
### Example 

#### IPv4
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
        "isis-area-id": "0001",
        "advertize-default-route": false
      },
      "auto-assign-ips": true,
      "auto-assign-super-cidr": [
          {"matchType":"satellite","super-cidr":"172.100.0.0/16"},
          {"matchType":"gateway","super-cidr":"172.101.0.0/16"},
          {"matchType":"user","super-cidr":"172.102.0.0/16"},
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
        },
        "L3-config": {
          "routing-metadata": {
            "advertize-default-route": true
          }
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
      }
    }
  }
}
```

#### IPv6
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
      "routing-module": "extra.isisv6",
      "routing-metadata": {
        "isis-area-id": "0001"
      },
      "auto-assign-ips": true,
      "auto-assign-super-cidr": [
          {"matchType":"satellite","super-cidr6":"2001:db8:100::/48"},
          {"matchType":"gateway","super-cidr6":"2001:db8:101::/48"},
          {"matchType":"user","super-cidr6":"2001:db8:102::/48"}
      ]
    }
  },
  ...
}
```
---

### Field Descriptions

#### `node-config-common`

* **Type**: object 
* **Requirement**: mandatory
* **Description**: Common configuration applied to all nodes unless overridden within a specific entry in `nodes`.

#### Per-Field Descriptions of `node-config-common` 

##### `type`

* **Type**: string 
* **Requirement**: optional, if not included in per-node configuration
* **Description**: Logical node type (recommended: `satellite`, `gateway`, `user`). Used for classification, visualization, and rule-based automatic IP assignment. Any string can be used.

##### `n_antennas`

* **Type**: integer 
* **Requirement**: optional
* **Description**: Number of antennas associated with the node. Informational only; not interpreted by control scripts.

##### `metadata`

* **Type**: object 
* **Requirement**: optional
* **Description**: User-defined structured metadata. Not interpreted by control scripts.

##### `image`

* **Type**: string 
* **Requirement**: optional, if not included in per-node configuration
* **Description**: Docker image used to instantiate the node container. Must be accessible from all worker hosts.

##### `sidecars`

* **Type**: array of strings 
* **Requirement**: optional
* **Description**: List of Docker images for sidecar containers to run alongside the main container. Currently not supported by control scripts.

##### `cpu-request`

* **Type**: string 
* **Requirement**: optional
* **Units / Format**: Docker-compatible CPU syntax (e.g., `100m`).
* **Description**: Requested CPU resources for container scheduling and relative priority under contention.

##### `mem-request`

* **Type**: string  
* **Requirement**: optional
* **Units / Format**: Binary units `KiB`, `MiB`, `GiB`, `TiB` (e.g., `200MiB`).
* **Description**: Requested memory for container scheduling and relative priority for OOM behavior (e.g., reservation semantics).

##### `cpu-limit`

* **Type**: string
* **Requirement**: optional
* **Units / Format**: Docker-compatible CPU syntax (e.g., `200m`).
* **Description**: Hard CPU cap enforced at runtime.

##### `mem-limit`

* **Type**: string
* **Requirement**: optional
* **Units / Format**: Binary units `KiB`, `MiB`, `GiB`, `TiB` (e.g., `400MiB`).
* **Description**: Hard memory cap enforced at runtime.

##### `L3-config`

* **Type**: object
* **Requirement**: optional, if not included in per-node configuration
* **Description**: Layer-3 configuration applied to VXLAN-based overlay links.

##### Per-Field Description of `L3-config`

###### `enable-netem`

* **Type**: boolean
* **Requirement**: optional, if not included in per-node configuration
* **Description**: Enables Linux `tc netem` enforcement of link characteristics (delay/loss/rate) defined in epoch files.

###### `enable-routing`

* **Type**: boolean
* **Requirement**: optional, if not included in per-node configuration
* **Description**: Enables IP routing over overlay (satellite) links using the specified routing module.

###### `routing-module`

* **Type**: string 
* **Requirement**: required if `enable-routing` is `true` and not included in per-node configuration
* **Description**: Identifier of the routing configuration Python module used by the node agent (see `routing-interface.md`).

###### `routing-metadata`

* **Type**: object
* **Requirement**: optional
* **Description**: Module-specific configuration stored in Etcd used by the routing module (e.g., IS-IS area ID).

###### `auto-assign-ips`

* **Type**: boolean
* **Requirement**: optional
* **Description**: Enables automatic assignment of overlay IP subnets to nodes. Each node is allocated a /30 subnet routed on overlay (satellite) links from the matching `auto-assign-super-cidr` rule based on its `type`. If disabled, nodes must specify their own `cidr` in the per-node configuration, or they will have no overlay IP addresses.

###### `auto-assign-super-cidr`

* **Type**: array of objects
* **Requirement**: mandatory if `auto-assign-ips` is `true`, not needed otherwise
* **Description**: Rules mapping node types to CIDR blocks from which /30 overlay subnets of nodes are sequentially allocated.

Each rule object:

* `matchType`

  * **Type**: string
  * **Requirement**: mandatory if `auto-assign-super-cidr` is present
  * **Description**: Node type to match (e.g., `satellite`, `gateway`, `user`, or `any` for a default fallback).
* `super-cidr`, `super-cidr6`

  * **Type**: string (CIDR notation)
  * **Requirement**: super-cidr and/or super-cidr6 must be present depending on the IP version used in the constellation
  * **Description**: Base CIDR block used to allocate sequential /30 (IPv4) or /126 (IPv6) overlay subnets for nodes of the matched type. The v4  `super-cidr` block must not overlap with underlay address space (e.g., worker `sat-vnet-super-cidr`) or host physical networks. Either `super-cidr` or `super-cidr6` can be omitted for single-stack operation, or both for dual-stack operation.

---

#### `epoch-config`

* **Type**: object
* **Requirement**: mandatory
* **Description**: Specifies where epoch files are located and how they are selected.

##### `epoch-dir`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Path to the directory containing epoch definition files.

##### `file-pattern`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Filename pattern used to select epoch files within `epoch-dir`.For instance, `NetSatBench-epoch*.json` matches all files starting with `NetSatBench-epoch` and ending with `.json`. Each file name, shoule contain a numerical integer suffix that indicates the order of the epochs, e.g., `NetSatBench-epoch0.json`, `NetSatBench-epoch1.json`, etc.

---

#### `nodes`

* **Type**: object
* **Requirement**: mandatory
* **Description**: Map from `node-name` to per-node configuration objects. Each node may override any field in `node-config-common` re-inserting the same field in the node object.

##### Per-Node Additional Fields

###### `worker`

* **Type**: string
* **Requirement**: optional
* **Description**: Explicit worker host on which the node container is deployed. If omitted, placement is computed automatically based on available worker capacity and node CPU/MEM requests. Must match a key in `worker-config.json`.

###### `cidr`, `cidr-v6`

* **Type**: string (CIDR notation, optional for forced IP addressing)
* **Requirement**: optional
* **Description**: Explicit /30 (IPv4) or /126 (IPv6) overlay subnet assigned to the node. Override any automatic assignment.

---

## Epoch Configuration File

### Overview

An epoch configuration file defines dynamic events that modify the constellation state at specific emulation times, including:

* overlay links to add, update, or delete; and
* commands to execute inside node containers.

Epoch files are loaded sequentially by the control logic (constellation-run) and applied at the specified emulation time with an offset equal to the difference between the current epoch time and the first epoch time. 

The epoch file names are expected to terminate with a numerical suffix that indicates their processing sequence. E.g., `NetSatBench-epoch0.json`, `NetSatBench-epoch1.json`, etc. 

The processing of epoch files modifies the link and command (run) state stored in Etcd and sat-agent of emulated nodes are promptly notified. 

The first epoch file should contain <u>all initial link definitions</u> required to establish connectivity between nodes at simulation start time.

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

### Field Descriptions

#### `time`

* **Type**: string (ISO-8601 timestamp)
* **Requirement**: mandatory
* **Description**: Absolute simulation time associated with the epoch (e.g., `2024-06-01T12:02:00Z`). The runtime applies epoch offsets relative to the first epoch.

---

#### `links-add`

* **Type**: array of objects 
* **Requirement**: optional
* **Description**: List of new **bidirectional** Layer-2 overlay links to create at the epoch time. Each link is implemented as a VXLAN tunnel between two node endpoints. Since links are bidirectional, it is not needed to specify both directions.

#### Per-Fied Descriptions of `links-add`

##### `endpoint1`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Logical name of the first endpoint node (must exist in `sat-config.json`).

##### `endpoint2`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Logical name of the second endpoint node.

##### `endpoint1_antenna`

* **Type**: integer
* **Requirement**: optional
* **Description**: Antenna index on the first endpoint node.

##### `endpoint2_antenna`
* **Type**: integer
* **Requirement**: optional
* **Description**: Antenna index on the second endpoint node.

##### `rate`

* **Type**: string
* **Requirement**: optional (if enable-netem is false in L3-config)
* **Units / Format**: Linux `tc netem` rate syntax (e.g., `10mbit`, `1gbit`).
* **Description**: Link bandwidth cap applied via `tc netem`.

##### `loss`

* **Type**: number
* **Requirement**: optional (if enable-netem is false in L3-config)
* **Units / Format**: Percentage in `[0, 100]`.
* **Description**: Packet loss probability applied via `tc netem` with random distribution.

##### `delay`

* **Type**: string
* **Requirement**: optional (if enable-netem is false in L3-config)
* **Units / Format**: Linux `tc netem` time syntax (e.g., `5ms`, `100ms`).
* **Description**: One-way link delay applied via `tc netem`.

##### `limit`
* **Type**: integer
* **Requirement**: optional
* **Units / Format**: Number of packets.
* **Description**: Maximum number of packets that can be queued in the `tc netem` buffer.

---

#### `links-update`

* **Type**: array of objects (optional)
* **Requirement**: optional
* **Description**: Updates to characteristics of existing overlay links. Only the parameters present in an entry are modified; unspecified parameters retain their previous values.

#### Per-Fied Descriptions of `links-update`
Fields are the same as in `links-add`.

---

### `links-del`

* **Type**: array of objects
* **Requirement**: optional
* **Description**: List of overlay links to remove at the epoch time. Removal tears down the corresponding VXLAN tunnel and associated traffic control configuration.

#### Per-Fied Descriptions of `links-del`
Each entry supports:

##### `endpoint1`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Logical name of the first endpoint node.

##### `endpoint2`

* **Type**: string
* **Requirement**: mandatory
* **Description**: Logical name of the second endpoint node.

Any additional fields (e.g., `rate`, `loss`, `delay`, `limit`) may be present but are ignored during deletion.

---

### `run`

* **Type**: object
* **Requirement**: optional
* **Description**: Shell commands to execute inside node containers at the epoch time.

Structure:

```json
"run": {
  "<node-name>": [
    "<command-1>",
    "<command-2>"
  ]
}
```

Per-entry:

* **Key** (`<node-name>`)

  * **Type**: string 
  * **Requirement**: mandatory, must match a node defined in `sat-config.json`)
  * **Description**: Target node in which commands are executed.
  * **Value** (list of commands)

  * **Type**: array of strings
  * **Requirement**: mandatory
  * **Description**: Ordered list of shell commands executed sequentially inside the container environment. Long-running processes should be launched in detached sessions (e.g., `screen`, `tmux`). Error-handling semantics are implementation-defined.

---

## Cross-File Consistency Requirements

* Worker names referenced in `sat-config.json` must exist in `worker-config.json`.
* All node names referenced in epoch files (`endpoint1`, `endpoint2`, and `run` keys) must correspond to nodes defined in `sat-config.json`.

```
