<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Cloud Deployment Notes <!-- omit in toc -->

</div>

## Table of Contents <!-- omit in toc -->

- [Overview](#overview)
- [OpenStack Cloud Deployment Notes](#openstack-cloud-deployment-notes)
- [Azure Cloud Deployment Notes](#azure-cloud-deployment-notes)


## Overview
This document provides specific notes and instructions for deploying NetSatBench in cloud environments, such as OpenStack and Azure. It covers the necessary network configurations and considerations to ensure proper connectivity between worker nodes and the control host.

## OpenStack Cloud Deployment Notes

- Create a virtual network containing multiple connected virtual machines (VMs) that act as **workers**; one of these VMs also serves as the **control host**.
- Ensure that the security group associated with the VMs allows SSH access to the control host from your local machine.
- Enable IP forwarding for the CIDR specified by `sat-vnet-super-cidr` on the network interfaces (ports) of each VM:
  - Navigate to **Networks → \<network\> → Ports**
  - Select the relevant port
  - Configure **Allowed Address Pairs**
  - Add an allowed address pair covering the `sat-vnet-super-cidr` range

---

## Azure Cloud Deployment Notes

- Create a virtual network containing multiple connected virtual machines (VMs) that act as **workers**; one of these VMs also serves as the **control host**.
- Ensure that the network security group (NSG) associated with the VMs allows SSH access to the control host from your local machine.
- Enable IP forwarding on all network interfaces of the worker VMs:
  - Navigate to **Network Interface → IP configurations → Enable IP forwarding**
- Create a **Route Table** and add a route to forward traffic destined for each worker’s `sat-vnet-cidr`:
  - Navigate to **Route table → Settings → Routes → Add**
  - Set **Next hop type** to *Virtual appliance*
  - Set the **Next hop address** to the corresponding worker’s `eth0` IP address
- Associate the route table with the subnet used by the worker VMs:
  - Navigate to **Route table → Settings → Subnets → Associate**

> **Note:** Although routing tables inside the worker VMs are configured automatically by the NetSatBench control scripts, Azure requires an explicit route table at the virtual network level to enable inter-VM connectivity across the `sat-vnet-cidr` subnets.

