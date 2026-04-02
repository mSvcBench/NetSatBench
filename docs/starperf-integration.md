
<div align="center">

<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# StarPerf Simulator Integration for NetSatBench

</div>

- [StarPerf Simulator Integration for NetSatBench](#starperf-simulator-integration-for-netsatbench)
  - [Overview](#overview)
  - [Installation](#installation)
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
NetSatBench can be integrated with the StarPerf_Simulator plugin to generate satellite system epoch files based on user-defined performance scenarios. This integration allows users to leverage StarPerf's capabilities for modeling and simulating satellite system performance, while seamlessly incorporating the generated epoch files into NetSatBench for emulation.

## Installation
To enable the integration, follow these steps:
1. Download the StarPerf_Simulator v2.0 

```bash
# init StarPerf_Simulator v2.0 NetSatBench 
git submodule update --init --recursive

```


## Workflow
1. Define the satellite constellation via XML StarPerf_Simulator input formats and store it in `generators/StarPerf_Simulator/config/XML_constellation/<constellation_name>.xml`.
2. Define users and ground stations via XML files as per StarPerf_Simulator specifications and store them in `generators/StarPerf_Simulator/config/users/<constellation_name>.xml` and `generators/StarPerf_Simulator/config/ground_stations/<constellation_name>.xml`.
3. Run `python3 nsb.py starperf-generate ...` from the NetSatBench repository root to generate the extended H5 file containing the snapshots of the satellite system with delay, rate, and loss values of all links. The file is stored under `generators/StarPerf_Simulator/data/...`.
4. Use the generated H5 file to create epoch files for NetSatBench emulation with `python3 nsb.py starperf-export ...`.
5. Optionally generate static Cesium output with `python3 nsb.py starperf-visualize ...` or launch the MATLAB desktop visualizer with `python3 nsb.py starperf-matlab-visualize ...`.

The `nsb.py` integration automatically runs the StarPerf tools from `generators/StarPerf_Simulator/`. Specifically, the command mapping is:

| `nsb.py` command | StarPerf entrypoint |
| --- | --- |
| `python3 nsb.py starperf-generate` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchGenerate.py` |
| `python3 nsb.py starperf-export` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchExport.py` |
| `python3 nsb.py starperf-visualize` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchVisualizer.py` |
| `python3 nsb.py starperf-matlab-visualize` | `generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchMatlabVisualizer.m` via `utils/nsb-starperf-matlab-visualize.py` |

## StarPerf Generate

The recommended entrypoint for H5 generation is `python3 nsb.py starperf-generate`. This command wraps StarPerf's NetSatBench generation tool and produces the H5 file containing the snapshots of the satellite system based on the XML constellation, users, and ground station definitions. It processes the input XML files, simulates the satellite system performance using StarPerf_Simulator, and outputs an H5 file that can be used for further processing and emulation in NetSatBench.

The script extend the original StarPerf_Simulator functionality by adding support for user-defined ground stations and users in the H5 file and adding rate and loss values to the generated snapshots, in addition to delay values. Addtional informatinon in the H5 file include the configuratin used to generate the H5 file and the type of nodes (sat,gs,usr) for each node in the system.

The H5 group structure of the generated H5 file is as follows:

```yaml
- info: # attributes are metadata about the generated H5 file
- type:
  - shell_X : [1..n_tot] # vector describing type of nodes in the shell n. X (sat, gs, usr)
- delay:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix with delay values of all links between nodes (i,j) in snapshot n. Y of shell n. X. A value of 0 indicates no link between the nodes, while a positive value indicates the delay in ms of the link.
- rate:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix with bit-rate values in in Mbit/s of all links between nodes (i,j) in snapshot n. Y of shell n. X. 
- loss:
  - shell_X :
    - snapshot_Y: [1..n_tot,1..n_tot] # square matrix with loss values in percentage of all links between nodes (i,j) in snapshot n. Y of shell n. X.
```
Whe a delay value (i,j) is greatehr than zero, as for StarPerf specification, there is a link between the object i and j in the snapshot.
The existence of inter-satellite-links (ISLs) is managed by the ISL connectivity plugin of StarPerf_Simulator, passed as an argument to `python3 nsb.py starperf-generate`.
The existence of links for ground stations and users is managed by `python3 nsb.py starperf-generate` in two steps:
- first, all possible links are generated based on the minimum elevation angle passed as input
- second, the generated set of possible links is modified according to antenna constraints by the selected extended antenna plugin

The compuation of rate and loss values for any link is based on the extended loss and rate plugins used by `python3 nsb.py starperf-generate`. 

The extended plugins are contained in the [ext_connectivity_plugin](generators/StarPerf_Simulator/kits/NetSatBench/ext_connectivity_plugin) directory. For their doculmentation, please refer to the docstrings in the dummy `pass_antenna, pass_loss, pass_rate` plugin files.

Refer to `python3 nsb.py starperf-generate --help` for more details on the input arguments and usage.

## StarPerf Export
The recommended entrypoint for exporting epoch files is `python3 nsb.py starperf-export`. It generates the `sat-config.json` and epoch files for NetSatBench emulation based on the extended H5 file generated by `python3 nsb.py starperf-generate`. To generate the `sat-config.json` file, the export step uses a common configuration file (`sat-config-common.json`) that contains the `node-config-common` section to be used in the final `sat-config.json` file.
Refer to `python3 nsb.py starperf-export --help` for more details on the input arguments and usage.


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
This XML file describes a constellation with 1 shell at an altitude of 1200 km, an orbit cycle of 6556 seconds, an inclination of 87.9 degrees, no phase shift, 12 orbits, and 49 satellites per orbit.

### Ground Stations Definition
OneWeb ground stations XML file [OneWeb.xml](generators/StarPerf_Simulator/config/ground_stations/OneWeb.xml) 
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
This XML file describes 5 ground stations located in Scanzano (Italy), Öjebyn (Sweden), Plana (Bulgaria), Svalbard (Norway) and Fucino (Italy) with their respective latitude, longitude, description, antenna count, frequency and uplink/downlink GHz values.


### Users Definition
OneWeb user XML file [OneWeb.xml](generators/StarPerf_Simulator/config/users/OneWeb.xml)
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
</USRs>
```
This XML file describes 6 users located in Rome (Italy), London (UK), Paris (France), Madrid (Spain), Athen (Greece) and Budapest (Hungary) with their respective latitude, longitude, name, antenna count and uplink/downlink GHz values.

### H5 Generation
To generate the H5 file with the snapshots of the satellite system, run the following command:
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

This command generates an H5 file with snapshots of the OneWeb constellation, including ground stations and users, with a timeslot interval of 5 seconds and a total simulation duration of 3600 seconds.

ISL connectivity is managed by the `positive_Grid` plugin (+Grid inter-satellite link model). Antenna constraints, link rate, and packet loss are handled by the `pass_antenna`, `slant_rate`, and `pass_loss` plugins, respectively.

In this configuration, all satellite-to-ground-station and satellite-to-user links above the minimum elevation threshold are considered valid candidates by the `pass_antenna` plugin. The `slant_rate` plugin reduces satellite-to-user and satellite-to-gateway bitrates from their default values of 50 Mbps and 200 Mbps, respectively, as a function of elevation angle. ISL links use the default rate of 400 Mbps through the `pass_rate` model. The `pass_loss` plugin assigns a loss value of `0.0` to all links.

The minimum elevation angle for satellite-to-ground visibility is set to 25 degrees, and existing information in the output H5 file will be overwritten if it already exists.


### Sat-config and Epoch File Generation
To generate the `sat-config.json` and epoch files for NetSatBench emulation based on the generated H5 file, run the following command:

```bash
python3 nsb.py starperf-export \
--h5 data/XML_constellation/OneWeb_ext.h5 \
--sat-config-common kits/NetSatBench/sat-config-common.json
```

This will generate the `sat-config.json` file and epoch files for NetSatBench emulation based on the extended H5 file generated in the previous step for the OneWeb constellation. The generated files are in the root `examples/SatPerf/<constellation_name>`
directory of the NetSatBench repository.

## Dynamic system visualization with MATLAB
The recommended entrypoint for MATLAB visualization is `python3 nsb.py starperf-matlab-visualize`. It launches the MATLAB visualizer for a selected shell by loading satellite trajectories from the generated HDF5 file and pairing them with the StarPerf XML constellation, user, and gateway definitions. It can optionally create HDF5-driven user, gateway access, ISL links (access objects) inside a `satelliteScenario` Matlab object.

NetSatBench exposes this visualizer through `python3 nsb.py starperf-matlab-visualize ...`. The wrapper launches a normal MATLAB desktop session because the visualization requires desktop UI support for `satelliteScenarioViewer`.

Inputs:
- constellation XML file
- user XML file
- gateway XML file
- extended HDF5 file generated by `python3 nsb.py starperf-generate`

Key name-value parameters:
- `"SelectedShell"` to choose which shell to visualize
- `"AddUserAccess"`, `"AddGatewayAccess"`, and `"AddISL"` to create link/access objects from the HDF5 connectivity
- `"StartTime"`, `"StopTime"`, and `"SampleTime"` to control the MATLAB scenario timing
- `"CacheFile"` and `"UseCache"` to reuse cached visualization data between runs

Direct MATLAB example:

```matlab
NetSatBenchMatlabVisualizer("../../config/XML_constellation/OneWeb.xml", ...
    "../../data/XML_constellation/OneWeb_ext.h5", ...
    "../../config/users/OneWeb.xml", ...
    "../../config/ground_stations/OneWeb.xml", ...
    "AddUserAccess", true, "CacheFile", "matlab_cache/OneWeb.mat");
```

This opens a MATLAB `satelliteScenarioViewer`, animates the selected shell, and prints a summary of the loaded constellation, terminals, and link sets. Only user links are added in this example, and the generated cache file is stored in `matlab_cache/OneWeb.mat` for reuse in future anumations.

Equivalent `nsb.py` example:

```bash
python3 nsb.py starperf-matlab-visualize \
  --constellation-name OneWeb \
  --h5 data/XML_constellation/OneWeb_ext.h5 \
  --add-user-access \
  --cache-file matlab_cache/OneWeb.mat
```

If MATLAB is not on your `PATH`, pass the executable explicitly:

```bash
python3 nsb.py starperf-matlab-visualize \
  --matlab-path /usr/local/MATLAB/R2024b/bin/matlab \
  --constellation-name OneWeb \
  --h5 data/XML_constellation/OneWeb_ext.h5
```

## Static system visualization with Cesium
The recommended entrypoint for Cesium visualization is `python3 nsb.py starperf-visualize`. It generates a Cesium HTML view of a constellation using the StarPerf XML configuration files.  
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
- Obtain a personal Cesium token and set it in `Cesium.Ion.defaultAccessToken` inside [/generators/StarPerf_Simulator/visualization/html_head_tail/head.html](../generators/StarPerf_Simulator/visualization/html_head_tail/head.html).


### Output <!-- omit in toc -->
The generated file is written to:
- `<outdir>/<constellation_name>_NetSatBench_without_ISL.html` (default behavior), or
- `<outdir>/<constellation_name>_NetSatBench_with_ISL.html` (when `--with-isl` is used).

Default output directory: `./visualization/CesiumAPP`.

### Coverage and ISL behavior <!-- omit in toc -->
- Without `--with-isl`:
  - satellite coverage circles are added, with radius dynamically computed from satellite altitude and `--minimum-elevation`.
- With `--with-isl`:
  - ISL links are drawn as polyline entities,
  - coverage circles are intentionally skipped to reduce clutter.
- The `--minimum-elevation` parameter (degree) is used to compute satellite coverage circles as a function of the minimum elevation angles used to determine satellite visibility from the earth.
  
### CLI arguments <!-- omit in toc -->
Run `python3 nsb.py starperf-visualize --help` for full details. Main options:
- `--constellation-name` (required)
- `--minimum-elevation` (default `25.0`)
- `--outdir` (default `./visualization/CesiumAPP`)
- `--satellite-color` (default `RED`)
- `--ground-station-color` (default `GREEN`)
- `--user-color` (default `YELLOW`)
- `--with-isl` (flag)

### Example <!-- omit in toc -->
```bash
python3 nsb.py starperf-visualize \
  --constellation-name OneWeb \
  --minimum-elevation 25 \
  --satellite-color RED \
  --ground-station-color GREEN \
  --user-color YELLOW \
  --outdir visualization/CesiumAPP
```

### Cesium Runtime Instructions <!-- omit in toc -->

1. Install `Node.js` (recommended version newer than v13), ensure it is in your `PATH`, then install `http-server` tool.
2. Start a local web server from the Cesium output directory:

```bash
cd visualization/CesiumAPP
http-server -p 8081
```

5. Open the generated page in your browser:
   - `http://127.0.0.1:8081/<filename>`
   - where `<filename>` is the generated visualization HTML file.
