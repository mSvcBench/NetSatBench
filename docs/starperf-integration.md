
<div align="center">

<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# StarPerf Simulator Integration for NetSatBench

</div>

- [StarPerf Simulator Integration for NetSatBench](#starperf-simulator-integration-for-netsatbench)
  - [Overview](#overview)
  - [Installation](#installation)
  - [Workflow](#workflow)
  - [NetSatBenchGenerate](#netsatbenchgenerate)
  - [NetSatBenchExport](#netsatbenchexport)
  - [Example](#example)
    - [Constellation Definition](#constellation-definition)
    - [Ground Stations Definition](#ground-stations-definition)
    - [Users Definition](#users-definition)
    - [H5 Generation](#h5-generation)
    - [Sat-config and Epoch File Generation](#sat-config-and-epoch-file-generation)
  
## Overview
NetSatBench can be integrated with the StarPerf_Simulator plugin to generate satellite system epoch files based on user-defined performance scenarios. This integration allows users to leverage StarPerf's capabilities for modeling and simulating satellite system performance, while seamlessly incorporating the generated epoch files into NetSatBench for emulation.

## Installation
To enable the integration, follow these steps:
1. Download the StarPerf_Simulator v2.0 

```bash
cd generators

# download and unzip the StarPerf_Simulator v2.0 release (no need to clone the entire repository)
wget https://github.com/SpaceNetLab/StarPerf_Simulator/archive/refs/heads/release/v2.0.zip

# unzip into StarPerf_Simulator-release-v2.0
unzip v2.0.zip

# copy the contents of the unzipped directory into StarPerf_Simulator to merge with the existing NetSatBench plugin files
cp -r StarPerf_Simulator-release-v2.0/* StarPerf_Simulator

# clean up the downloaded zip and unzipped directory
rm v2.0.zip
rm -rf StarPerf_Simulator-release-v2.0

# optionally install dependencies into the NetSatBench Python environment if needed 
# pip install -r StarPerf_Simulator/docs/third-party_libraries_list.txt

```

> **Note**: the StarPerf_Simulator we used is v2.0 at commit 009f2eb722c52b621a038904246be76ae906d993

## Workflow
1. move to the `generators/StarPerf_Simulator` directory.
2. Define the satellite constellation via XML StarPerf_Simulator input formats and store it in the `/config/XML_constellation/<constellation_name>.XML`
3. Define users and ground station via XML file as per StarPerf_Simulator specifications and store them into `/config/users/<constellation_name>.XML` and `/config/ground_stations/<constellation_name>.XML` respectively.
4. Run the `NetSatBenchKit/NetSatBenchGenerate.py` plugin to generate the extended H5 file containing the snapshots of the satellite system with delay, rate and loss values of all links. The file is stored in `/data/XML_constellation/<constellation_name>_ext.h5`
5. Use the generated H5 file to create epoch files for NetSatBench emulation with `NetSatBenchKit/NetSatBenchExport.py`

## NetSatBenchGenerate

The `NetSatBenchGenerate.py` script is responsible for generating the H5 file containing the snapshots of the satellite system based on the XML constellation, users and ground stations definitions. It processes the input XML files, simulates the satellite system performance using StarPerf_Simulator, and outputs an H5 file that can be used for further processing and emulation in NetSatBench.

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
The existence of inter-satellite-links (ISLs) is managed by ISL connectivity plugin of StarPerf_Simulator, passed as argument to `NetSatBenchGenerate.py`.
The existence of links for ground stations and users is managed by `NetSatBenchGenerate.py` in two steps:
- first, all possible links are generated based on the minimum elevation angle passed passed as input to `NetSatBenchGenerate.py` 
- second, the generated set of possible links is modified according to antenna constraints by extended antenna plugin used by `NetSatBenchGenerate.py` and passed as argument.

The compuation of rate and loss values for any link is based on extended loss and rate plugins used by the `NetSatBenchGenerate.py` script. 

The extended plugins are contained in the [ext_connectivity_plugin](generators/StarPerf_Simulator/NetSatBenchKit/ext_connectivity_plugin) directory. For their doculmentation, please refer to the docstrings in the dummy `pass_antenna, pass_loss, pass_rate` plugin files.

Refer to the `NetSatBenchGenerate.py --help` script for more details on the input arguments and usage.

## NetSatBenchExport
The `NetSatBenchExport.py` script is responsible for generating the `sat-config.json` and epoch files for NetSatBench emulation based on the extended H5 file generated by `NetSatBenchGenerate.py`. It processes the H5 file, extracts the relevant snapshots, and creates epoch files that can be used for emulation in NetSatBench. To generate the `sat-config.json` file, the script uses a common configuration file (`sat-config-common.json`) that contains the `node-config-common` section to be used in the final `sat-config.json` file.
Refer to the `NetSatBenchExport.py --help` script for more details on the input arguments and usage.


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
python3 NetSatBenchKit/NetSatBenchGenerate.py \
--constellation OneWeb \
--dT 15 \
--isl-connectivity-plugin positive_Grid \
--gs-antenna-plugin retain_antenna \
--user-antenna-plugin retain_antenna \
--isl-rate-plugin pass_rate \
--gs-rate-plugin pass_rate \
--user-rate-plugin pass_rate \
--isl-loss-plugin pass_loss \
--gs-loss-plugin pass_loss \
--user-loss-plugin pass_loss \
--isl-rate 100 \
--gs-rate 100 \
--user-rate 50 \
--loss-isl 0.0 \
--loss-gs 0.0 \
--loss-user 0.0 \
--include-ground-stations \
--include-users \
--minimum-elevation 25 \
--overwrite
```
This command generates an H5 file with snapshots of the OneWeb constellation, including ground stations and users, with a timeslot interval of 15 seconds. 
The ISL connectivity is managed by the `positive_Grid` plugin (+Grid Inter-Satellite Link model), while the antenna limitations, rate and loss characteristics for links are managed by the `retain_antenna`, `pass_rate` and `pass_loss` plugins respectively. 
In this case, all visible ground station and user links over minimum elevation angle are possible candidate, and the antenna plugin prefers to retain old links for limitation in the number of antennas (see also the `retain_antenna` plugin documentation in the pythoin file). 
The pass_rate and pass_loss plugins are used to assign default rate and loss values to all links.
The default rate values are set to 100 Mbit/s for ISL and ground station links, and 50 Mbit/s for user links, while the default loss values are set to 0% for all links. The minimum elevation angle for visibility is set to 25 degrees, and existing information in the output H5 file will be overwritten if they already exist.

### Sat-config and Epoch File Generation
To generate the `sat-config.json` and epoch files for NetSatBench emulation based on the generated H5 file, run the following command:

```bash
python3 NetSatBenchKit/NetSatBenchExport.py \
--h5 data/XML_constellation/OneWeb_ext.h5 \
--sat-config-common NetSatBenchKit/sat-config-common.json
```

This will generate the `sat-config.json` file and epoch files for NetSatBench emulation based on the extended H5 file generated in the previous step for the OneWeb constellation. The generated files are in the root `examples/SatPerf/<constellation_name>`
directory of the NetSatBench repository.