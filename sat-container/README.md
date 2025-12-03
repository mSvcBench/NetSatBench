Internal Container Logic (sat-container/)
These files are copied into the Docker image during the build and define the container's lifecycle and networking behavior:

•	Dockerfile: Builds the simulation image based on ubuntu:22.04. It installs core networking tools (frr, tcpdump, iproute2), Python dependencies (etcd3, protobuf), and the SSH server. It sets create_bridges.sh as the container entrypoint.

•	create_bridges.sh: The Entrypoint Script. It initializes the container environment by starting the SSH daemon and waiting for the Etcd database to become reachable. It dynamically determines the number of antennas (bridges) needed based on the hostname (5 for satellites, 1 for ground stations), creates those bridges (brX), and then execs the persistent Python agent.

•	sat-agent-internal.py: The Main Event Loop. This multi-threaded Python script runs continuously.
  
    o	Thread 1 (L2 Topology): Watches Etcd for link changes. It manages the Layer 2 Data Plane by creating VXLAN tunnels (via update-link-internal.sh) and bridging them   to local antennas. This creates the virtual "wires" between nodes at the Ethernet level.
  
    o	Thread 2 (Runtime): Watches Etcd for command queues (/config/run/) and executes shell commands inside the container.

•	configure-isis.sh: An Optional configuration utility. It is invoked externally (usually by network-configuration.py) only if routing is enabled. It writes the FRRouting (FRR) configuration to establish Layer 3 routing (IS-IS) over the Layer 2 VXLAN bridges.

