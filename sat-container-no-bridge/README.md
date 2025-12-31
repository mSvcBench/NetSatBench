## Internal Container Logic (`sat-container/`)

This directory contains the files copied into the Docker image during the build process. These scripts define the container's lifecycle, initialization, and networking behavior.

### File Overview

####  `Dockerfile`
Builds the simulation image based on `ubuntu:22.04`.
* **Installs dependencies:** Core networking tools (`frr`, `tcpdump`, `iproute2`), Python packages (`etcd3`, `protobuf`), and the SSH server.
* **Sets Entrypoint:** Configures `create_bridges.sh` as the container entrypoint.

####  `create_bridges.sh` (Entrypoint Script)
Initializes the container environment on startup.
1.  Starts the SSH daemon.
2.  Waits for the Etcd database to become reachable.
3.  **Dynamic Configuration:** checks the hostname to determine the number of antennas (bridges) needed (e.g., 5 for satellites, 1 for ground stations).
4.  Creates the necessary bridges (`brX`) and then `exec`s the persistent Python agent.

####  `sat-agent-internal.py` (Main Event Loop)
A multi-threaded Python script that runs continuously to manage node behavior.
* **Thread 1 (L2 Topology):** Watches Etcd for link changes. It manages the Layer 2 Data Plane by creating VXLAN tunnels (calling `update-link-internal.sh`) and bridging them to local antennas. This creates the virtual "wires" connecting nodes via Ethernet.
* **Thread 2 (Runtime):** Watches Etcd for command queues at `/config/run/` and executes shell commands inside the container.

####  `configure-isis.sh` (Routing Configuration)
An optional configuration utility invoked externally (usually by `network-configuration.py`) when routing is enabled.
* **Function:** Writes the FRRouting (FRR) configuration to establish Layer 3 routing (IS-IS) over the existing Layer 2 VXLAN bridges.
