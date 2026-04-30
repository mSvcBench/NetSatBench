<div align="center">

<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# Scenario Generation based on StarPerf_Simulator <!-- omit in toc -->

</div>

- [Overview](#overview)
- [Workflow](#workflow)
- [StarPerf Generate](#starperf-generate)
- [StarPerf Export](#starperf-export)
- [Example](#example)
  - [Constellation Definition](#constellation-definition)
  - [Ground Stations Definition](#ground-stations-definition)
  - [Users Definition](#users-definition)
  - [H5 Generation](#h5-generation)
  - [Sat-config and Epoch File Generation](#sat-config-and-epoch-file-generation)
- [Dynamic system visualization with MATLAB](#dynamic-system-visualization-with-matlab)
- [Static system visualization with Cesium](#static-system-visualization-with-cesium)

## Overview
NetSatBench integrates with [StarPerf_Simulator](https://github.com/SpaceNetLab/StarPerf_Simulator) to generate configuration (`sat-config.json`) and epoch files. This integration extends the original StarPerf_Simulator by adding support for user-defined gateway ground stations and users in the generated H5 file, and by adding rate and loss values to snapshots in addition to delay values. These link values can also be controlled through user-defined physical-layer plug-ins passed as arguments to the generation step.

StarPerf_Simulator is included in `generators/StarPerf_Simulator/` within the NetSatBench repository. The NetSatBench extension wrapper is implemented in `generators/StarPerf_Simulator/kits/NetSatBench/`.

## Workflow
1. Define the satellite constellation using StarPerf_Simulator XML input format and store it in `generators/StarPerf_Simulator/config/XML_constellation/<constellation_name>.xml`.
For example, the OneWeb constellation is defined in [generators/StarPerf_Simulator/config/XML_constellation/OneWeb.xml](generators/StarPerf_Simulator/config/XML_constellation/OneWeb.xml).
2. Define users and ground stations using StarPerf_Simulator XML format and store them in `generators/StarPerf_Simulator/config/users/<constellation_name>.xml` and `generators/StarPerf_Simulator/config/ground_stations/<constellation_name>.xml`.
For example, OneWeb users and ground stations are defined in [generators/StarPerf_Simulator/config/users/OneWeb.xml](generators/StarPerf_Simulator/config/users/OneWeb.xml) and [generators/StarPerf_Simulator/config/ground_stations/OneWeb.xml](generators/StarPerf_Simulator/config/ground_stations/OneWeb.xml).
3. Run `python3 nsb.py starperf-generate ...` from the NetSatBench repository root to generate an extended H5 file containing system snapshots with delay, rate, and loss values for all links. The output is stored under `generators/StarPerf_Simulator/data/XML_constellation/` with suffix `_ext.h5` (original StarPerf output does not use `_ext`).
4. Use the generated extended H5 file to create NetSatBench configuration and epoch files with `python3 nsb.py starperf-export ...`.
5. Optionally generate static Cesium output with `python3 nsb.py starperf-visualize ...` or launch the MATLAB desktop visualizer with `python3 nsb.py starperf-matlab-visualize ...`.

The `nsb.py` integration automatically executes StarPerf tools from `generators/StarPerf_Simulator/`. Command mapping:

| `nsb.py` command | StarPerf entrypoint |
| --- | --- |
| `python3 nsb.py starperf-generate` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchGenerate.py` |
| `python3 nsb.py starperf-export` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchExport.py` |
| `python3 nsb.py starperf-matlab-visualize` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchMatlabVisualizer.m` via `utils/nsb-starperf-matlab-visualize.py` |

## StarPerf Generate

The recommended entrypoint for extended H5 generation is `python3 nsb.py starperf-generate`. This command wraps StarPerf's NetSatBench generation tool and produces an H5 file containing snapshots of the satellite system based on XML constellation, user, and ground-station definitions. It processes input XML files, simulates satellite system behavior using StarPerf_Simulator, and outputs an extended H5 file (terminating with `_ext.h5`) with the following structure:

```yaml
- info: # attributes are metadata about the generated H5 file
- type:
  - shell_X : [1..n_tot] # vector describing node type in shell X (sat, gs, usr)
- delay:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix of link delay values (ms) between nodes (i,j) at snapshot Y of shell X. 0 means no link.
- rate:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix of link bit rates (Mbit/s) between nodes (i,j) at snapshot Y of shell X.
- loss:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix of link loss values (%) between nodes (i,j) at snapshot Y of shell X.
```

When delay `(i,j)` is greater than zero, a link exists between nodes `i` and `j` in that snapshot.

Inter-satellite link (ISL) existence is managed by the StarPerf_Simulator ISL connectivity plug-in passed to `python3 nsb.py starperf-generate`.

Ground-station and user link existence is managed by `python3 nsb.py starperf-generate` in two steps:
- first, all possible links are generated based on the input minimum elevation angle;
- second, that candidate set is filtered according to antenna constraints by the selected physical-layer antenna plug-in.

Rate and loss values for each link are computed using the physical-layer rate and loss plug-ins selected for `python3 nsb.py starperf-generate`.

Extended plug-ins are located in [ext_connectivity_plugin](generators/StarPerf_Simulator/kits/NetSatBench/ext_connectivity_plugin). For details, refer to the docstrings in the dummy plug-ins `pass_antenna`, `pass_loss`, and `pass_rate`.

Refer to `python3 nsb.py starperf-generate --help` for complete argument documentation.

## StarPerf Export
After generating the extended H5 file, the next step is to create NetSatBench configuration and epoch files with `python3 nsb.py starperf-export`. This command processes the extended H5 file and produces a `sat-config.json` file plus epoch files for NetSatBench emulation.

To generate `sat-config.json`, the export step uses a common configuration file (`sat-config-common.json`) that provides the `node-config-common` section used in the final `sat-config.json`.

Refer to `python3 nsb.py starperf-export --help` for complete argument documentation.

## Example
### Constellation Definition
OneWeb constellation XML file [OneWeb.xml](generators/StarPerf_Simulator/config/XML_constellation/OneWeb.xml):
```xml
<constellation>
    <number_of_shells>1</number_of_shells>
    <shell1>
        <altitude>1200</altitude>
        <orbit_cycle>6556</orbit_cycle>
        <inclination>87.9</inclination>
        <phase_shift>0</phase_shift>
        <number_of_orbit>12</number_of_orbit>
        <number_of_satellite_per_orbit>49</number_of_satellite_per_orbit>
    </shell1>
</constellation>
```
This XML file describes a constellation with 1 shell at 1200 km altitude, 6556-second orbit cycle, 87.9-degree inclination, no phase shift, 12 orbits, and 49 satellites per orbit.

### Ground Stations Definition
OneWeb ground-station XML file [OneWeb.xml](generators/StarPerf_Simulator/config/ground_stations/OneWeb.xml):
```xml
<GSs>
  <GS1>
    <Latitude>37.913637773977435</Latitude>
    <Longitude>13.362289636513902</Longitude>
    <Description>Scanzano (Italy)</Description>
    <Antenna_Count>8</Antenna_Count>
    <Frequency>Ka</Frequency>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </GS1>
  <GS2>
    <Latitude>65.34334207699979</Latitude>
    <Longitude>21.396971500146204</Longitude>
    <Description>Öjebyn (Sweden)</Description>
    <Antenna_Count>8</Antenna_Count>
    <Frequency>Ka</Frequency>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </GS2>
  <GS3>
    <Latitude>42.48875195640523</Latitude>
    <Longitude>23.408190420645646</Longitude>
    <Description>Plana (Bulgaria)</Description>
    <Antenna_Count>8</Antenna_Count>
    <Frequency>Ka</Frequency>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </GS3>
  <GS4>
    <Latitude>78.24871490544288</Latitude>
    <Longitude>15.490615165467464</Longitude>
    <Description>Svalbard (Norway)</Description>
    <Antenna_Count>8</Antenna_Count>
    <Frequency>Ka</Frequency>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </GS4>
  <GS5>
    <Latitude>42.02097199610034</Latitude>
    <Longitude>13.636767797109698</Longitude>
    <Description>Fucino (Italy)</Description>
    <Antenna_Count>8</Antenna_Count>
    <Frequency>Ka</Frequency>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </GS5>
</GSs>
```
This XML file defines 5 ground stations: Scanzano (Italy), Öjebyn (Sweden), Plana (Bulgaria), Svalbard (Norway), and Fucino (Italy), including latitude/longitude, description, antenna count, frequency, and uplink/downlink GHz values.

### Users Definition
OneWeb user XML file [OneWeb.xml](generators/StarPerf_Simulator/config/users/OneWeb.xml):
```xml
<USRs>
  <USR1>
    <Latitude>41.9028</Latitude>
    <Longitude>12.4964</Longitude>
    <Name>Rome (Italy)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR1>
  <USR2>
    <Latitude>51.5074</Latitude>
    <Longitude>-0.1278</Longitude>
    <Name>London (UK)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR2>
  <USR3>
    <Latitude>48.8566</Latitude>
    <Longitude>2.3522</Longitude>
    <Name>Paris (France)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR3>
  <USR4>
    <Latitude>40.4168</Latitude>
    <Longitude>-3.7038</Longitude>
    <Name>Madrid (Spain)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR4>
  <USR5>
    <Latitude>37.9838</Latitude>
    <Longitude>23.7275</Longitude>
    <Name>Athen (Greece)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR5>
  <USR6>
    <Latitude>47.4979</Latitude>
    <Longitude>19.0402</Longitude>
    <Name>Budapest (Hungary)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR6>
  <USR7>
    <Latitude>41.387082</Latitude>
    <Longitude>-24.347609</Longitude>
    <Name>Vessel (Atlantic Ocean)</Name>
    <Antenna_Count>2</Antenna_Count>
    <Uplink_Ghz>2.1</Uplink_Ghz>
    <Downlink_Ghz>1.3</Downlink_Ghz>
  </USR7>
</USRs>
```
This XML file defines 7 users: Rome (Italy), London (UK), Paris (France), Madrid (Spain), Athen (Greece), Budapest (Hungary), and a vessel in the Atlantic Ocean, including latitude/longitude, name, antenna count, and uplink/downlink GHz values.

### H5 Generation
To generate the extended H5 file with system snapshots, run:
```bash
python3 nsb.py starperf-generate \
--constellation-name OneWeb \
--dT 5 \
--isl-connectivity-plugin positive_Grid \
--gs-antenna-plugin pass_antenna \
--user-antenna-plugin pass_antenna \
--isl-rate-plugin pass_rate \
--gs-rate-plugin slant_rate \
--user-rate-plugin slant_rate \
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

This command generates an extended H5 file for the OneWeb constellation, including ground stations and users, with a 5-second timeslot and total simulation duration of 3600 seconds.

ISL connectivity is managed by `positive_Grid` (+Grid ISL model). Antenna constraints, link rate, and packet loss are handled by `pass_antenna`, `slant_rate`, and `pass_loss`, respectively.

In this setup, all satellite-to-ground-station and satellite-to-user links above the minimum elevation threshold are considered valid connection candidates by the dummy `pass_antenna` plug-in. The `slant_rate` plug-in reduces satellite-to-user and satellite-to-gateway bitrates from default values (50 Mbps and 200 Mbps) as a function of slant distance between the satellite and ground device (see reference paper in [README](../README.md)). ISL links use a default 400 Mbps rate through `pass_rate`. The `pass_loss` plug-in assigns `0.0` loss to all links.

The minimum elevation angle for satellite-to-ground visibility is set to 25 degrees. If output H5 data already exists, `--overwrite` replaces it.

### Sat-config and Epoch File Generation
To generate `sat-config.json` and epoch files for NetSatBench emulation from the generated H5 file, run:

```bash
python3 nsb.py starperf-export \
--h5 data/XML_constellation/OneWeb_ext.h5 \
--sat-config-common kits/NetSatBench/sat-config-common.json
```

This generates `sat-config.json` and epoch files for NetSatBench emulation based on the previously generated extended H5 file for OneWeb. Output is placed under `examples/SatPerf/<constellation_name>` at repository root.

Note: `sat-config-common.json` is only an example configured for IPv4 + IS-IS and may not scale well for a constellation as large as OneWeb. For scalable settings, see [test/handover/README.md](../test/handover/README.md).

## Dynamic system visualization with MATLAB
Run `python3 nsb.py starperf-matlab-visualize` to launch the MATLAB visualizer for a selected shell. It loads satellite trajectories from the generated HDF5 file together with StarPerf XML constellation, user, and gateway definitions. Optionally, it can create H5-driven user/gateway access links and ISL links (access objects) in a MATLAB `satelliteScenario` object.

Example:

```bash
python3 nsb.py starperf-matlab-visualize \
  --constellation-name OneWeb \
  --h5 data/XML_constellation/OneWeb_ext.h5 \
  --add-user-access \
  --cache-file matlab_cache/OneWeb.mat
```

If MATLAB is not in your `PATH`, pass the executable explicitly:

```bash
python3 nsb.py starperf-matlab-visualize \
  --matlab-path /usr/local/MATLAB/R2024b/bin/matlab \
  --constellation-name OneWeb \
  --h5 data/XML_constellation/OneWeb_ext.h5
```

## Static system visualization with Cesium
The recommended entrypoint for Cesium visualization is `python3 nsb.py starperf-visualize`. It generates a Cesium HTML view of a constellation using StarPerf XML configuration files.

It renders:
- satellites (as 3D points),
- ground stations (from `config/ground_stations/<constellation>.xml`),
- user terminals (from `config/users/<constellation>.xml`),
- optional ISL lines.

The visualization is static and computed at epoch `1949-10-01 00:00:00` for all satellites.

### Inputs <!-- omit in toc -->
- Constellation XML: `config/XML_constellation/<constellation_name>.xml`
- Ground stations XML: `config/ground_stations/<constellation_name>.xml`
- Users XML: `config/users/<constellation_name>.xml`
- Obtain a personal Cesium token and set it in `Cesium.Ion.defaultAccessToken` in [/generators/StarPerf_Simulator/visualization/html_head_tail/head.html](../generators/StarPerf_Simulator/visualization/html_head_tail/head.html).

### Output <!-- omit in toc -->
Generated file path:
- `<outdir>/<constellation_name>_NetSatBench_without_ISL.html` (default), or
- `<outdir>/<constellation_name>_NetSatBench_with_ISL.html` (with `--with-isl`).

Default output directory: `./visualization/CesiumAPP`.

### Coverage and ISL behavior <!-- omit in toc -->
- Without `--with-isl`:
  - satellite coverage circles are added, with radius computed from satellite altitude and `--minimum-elevation`.
- With `--with-isl`:
  - ISL links are drawn as polyline entities,
  - coverage circles are intentionally skipped to reduce clutter.
- `--minimum-elevation` (degrees) is used to compute satellite coverage circles according to the minimum elevation angle used for satellite visibility from Earth.
