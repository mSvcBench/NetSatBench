#!/usr/bin/env python3
"""
StarPerf 2.0 constellation generation with extended h5 data 

Pipeline:
1) Build constellation (XML or TLE)
2) ISL connectivity (positive_Grid by default)
3) Add ground stations and users links
4) Extend h5 with delay/rate/loss/type datasets for ISL, GS, and user links

Conventions:
- The output .h5 file must contain groups: /position_ext, /delay_ext, /type_ext, /rate_ext, /loss_ext. Each group should have the same internal structure as the input /position and /delay groups produced by StarPerf, with datasets named timeslot1, timeslot2, etc. corresponding to each timeslot.
- First index are for satellites (starting at 0, followed by ground stations and users in the order they are defined in the input XML files if included.
- The position_ext datasets has three columns for X/Y/Z ECEF coordinates. 
- The delay_ext datasets is a square matrix where the value at [i,j] is the delay from node i to node j in seconds, or 0 if no link exists.
- The rate_ext datasets is a square matrix where the value at [i,j] is the link rate in Mbps.
- The loss_ext datasets is a square matrix where the value at [i,j] is the loss rate as a float between 0 and 1.
- The type_ext datasets is a vector where the value at [i] is a string indicating the node type ("sat", "gs", or "user").
"""

import argparse
from pathlib import Path
import sys

import h5py
import numpy as np
import math
import xml.etree.ElementTree as ET
import sys
# import from upper dir for constellation generation and connectivity plugins
sys.path.append(str(Path(__file__).parent.parent))  # adjust as needed
import src.XML_constellation.constellation_entity.ground_station as GS
import src.XML_constellation.constellation_entity.satellite as SAT
import src.XML_constellation.constellation_entity.user as USER
import re
from typing import List, Tuple, Dict, Optional
import importlib

# ----------------------- 
# HELPERS
# -----------------------
## Read xml document
def xml_to_dict(element):
    if len(element) == 0:
        return element.text
    result = {}
    for child in element:
        child_data = xml_to_dict(child)
        if child.tag in result:
            if type(result[child.tag]) is list:
                result[child.tag].append(child_data)
            else:
                result[child.tag] = [result[child.tag], child_data]
        else:
            result[child.tag] = child_data
    return result

## Read xml document
def read_xml_file(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    return {root.tag: xml_to_dict(root)}

## Convert lat/long/alt to ECEF coordinates
def latilong_to_descartes(transformed_object):
    a = 6371000.0  # Earth's equatorial radius in meters
    e2 = 0.00669438002290 # Square of Earth's eccentricity
    longitude = math.radians(transformed_object.longitude)
    latitude = math.radians(transformed_object.latitude)
    fac1 = 1 - e2 * math.sin(latitude) * math.sin(latitude)
    N = a / math.sqrt(fac1)
    # the unit of satellite height above the ground is meters
    h = transformed_object.altitude * 1000
    X = (N + h) * math.cos(latitude) * math.cos(longitude)
    Y = (N + h) * math.cos(latitude) * math.sin(longitude)
    Z = (N * (1 - e2) + h) * math.sin(latitude)
    return X, Y, Z

## Judge if satellite can see the point on the ground based on minimum elevation angle
def judgePointToSatellite(sat_x , sat_y , sat_z , point_x , point_y , point_z , minimum_elevation):
    A = 1.0 * point_x * (point_x - sat_x) + point_y * (point_y - sat_y) + point_z * (point_z - sat_z)
    B = 1.0 * math.sqrt(point_x * point_x + point_y * point_y + point_z * point_z)
    C = 1.0 * math.sqrt(math.pow(sat_x - point_x, 2) + math.pow(sat_y - point_y, 2) + math.pow(sat_z - point_z, 2))
    angle = math.degrees(math.acos(A / (B * C))) # calculate angles and convert radians to degrees
    if angle < 90 + minimum_elevation or math.fabs(angle - 90 - minimum_elevation) <= 1e-6:
        return False, angle
    else:
        return True, angle

# Find the corresponding satellite according to the satellite's ID.
def search_satellite_by_id(sh , target_id):
    # the total number of satellites contained in the sh layer shell
    number_of_satellites_in_sh = sh.number_of_satellites
    # the total number of tracks contained in the sh layer shell
    number_of_orbits_in_sh = sh.number_of_orbits
    # in the sh layer shell, the number of satellites contained in each orbit
    number_of_satellites_per_orbit = (int)(number_of_satellites_in_sh / number_of_orbits_in_sh)
    # find the corresponding satellite according to the satellite id
    # traverse each orbit layer by layer, orbit_index starts from 1
    for orbit_index in range(1, number_of_orbits_in_sh + 1, 1):
        # traverse the satellites in each orbit, satellite_index starts from 1
        for satellite_index in range(1, number_of_satellites_per_orbit + 1, 1):
            satellite = sh.orbits[orbit_index - 1].satellites[satellite_index - 1]  # get satellite object
            if satellite.id == target_id :
                return satellite

## Create GroundStation and User objects from XML
def read_ground_stations_xml(gs_xml_path: Path) -> List[GS.GroundStation]:
    """
    Parse StarPerf ground station XML (config/ground_stations/<Constellation>.xml).
    Returns list of (lat_deg, lon_deg, alt_m, name).
    The interface convention states each GS has Latitude/Longitude plus metadata. fileciteturn4file10
    """
    # read ground base station data
    ground_station = read_xml_file(gs_xml_path)
    # generate GS
    GSs = []
    for gs_count in range(1, len(ground_station['GSs']) + 1, 1):
        gs = GS.ground_station(longitude=float(ground_station['GSs']['GS' + str(gs_count)]['Longitude']),
                                latitude=float(ground_station['GSs']['GS' + str(gs_count)]['Latitude']),
                                description=ground_station['GSs']['GS' + str(gs_count)]['Description'],
                                frequency=ground_station['GSs']['GS' + str(gs_count)]['Frequency'],
                                antenna_count=int(ground_station['GSs']['GS' + str(gs_count)]['Antenna_Count']),
                                uplink_GHz=float(ground_station['GSs']['GS' + str(gs_count)]['Uplink_Ghz']),
                                downlink_GHz=float(ground_station['GSs']['GS' + str(gs_count)]['Downlink_Ghz']))
        GSs.append(gs)
    return GSs

def read_users_xml(users_xml_path: Path) -> List[USER.user]:
    """
    Parse StarPerf user XML (config/users/<Constellation>.xml).
    Returns list of (lat_deg, lon_deg, alt_m, name).
    The interface convention states each user has Latitude/Longitude plus metadata. fileciteturn4file10
    """
    # read ground base station data
    users = read_xml_file(users_xml_path)
    # generate USER
    USERs = []
    for user_count in range(1, len(users['USRs']) + 1, 1):
        user = USER.user(longitude=float(users['USRs']['USR' + str(user_count)]['Longitude']),
                                latitude=float(users['USRs']['USR' + str(user_count)]['Latitude']),
                                user_name=users['USRs']['USR' + str(user_count)]['Name'])
        USERs.append(user)
    return USERs

# -----------------------
# MAIN FUNCTION
# -----------------------

def create_exteded_h5(
    h5_path: Path,
    SATs: List[SAT.satellite],
    GSs: List[GS.ground_station],
    USERs: List[USER.user],
    min_elevation_deg: float,
    sat_ext_conn_function: Optional[Dict[str, callable]] = None,
    gs_ext_conn_function: Optional[callable] = None,
    usr_ext_conn_function: Optional[callable] = None,
    rate: Optional[dict] = None,
    loss: Optional[dict] = None,
    overwrite: bool = False,
    dT: Optional[int] = None
) -> None:
    
    if not GSs:
        print("📡 Empty ground station list; check your ground station XML or command line arguments.")
    else:
        print(f"📡 Adding {len(GSs)} ground stations")
    
    if not USERs:
        print("👤 Empty user list; check your user XML or command line arguments.")
    else:
        print(f"👤 Adding {len(USERs)} users")
    
    if not SATs:
        print("🛰️ Satellite list is empty; check your StarPerf constellation initialization")
        exit(1)

    with h5py.File(h5_path, "a") as f:
        if "position" not in f or "delay" not in f:
            raise RuntimeError("Expected 'position' and 'delay' groups in the .h5 produced by StarPerf.")

        h5_pos_root = f["position"]
        h5_del_root = f["delay"]
        
        # Detect layout: if /position contains timeslot datasets -> single-shell, else treat children as shells.
        def _is_timeslot_name(name: str) -> bool:
            return bool(re.match(r"^timeslot\d+$", name))

        pos_children = list(h5_pos_root.keys())
        single_shell = any(_is_timeslot_name(k) for k in pos_children)

        # Create / reuse output groups at root
        def _ensure_group_at_root(name: str):
            if name in f:
                if overwrite:
                    del f[name]
                    return f.create_group(name)
                return f[name]
            return f.create_group(name)
        
        h5_type_root_ext = _ensure_group_at_root("type_ext")
        h5_rate_root_ext = _ensure_group_at_root("rate_ext")
        h5_loss_root_ext = _ensure_group_at_root("loss_ext")
        h5_pos_root_ext = _ensure_group_at_root("position_ext")
        h5_del_root_ext = _ensure_group_at_root("delay_ext")

        if single_shell:
            process_one_shell(shell_name=None,
                                  GSs=GSs,
                                  USERs=USERs,
                                  SATs=SATs,
                                  h5_pos_root=h5_pos_shell, h5_del_root=h5_del_shell,
                                  h5_pos_root_ext=h5_pos_shell_ext, h5_del_root_ext=h5_del_shell_ext, h5_type_root_ext=h5_type_shell_ext, h5_rate_root_ext=h5_rate_shell_ext, h5_loss_root_ext=h5_loss_shell_ext,
                                  gs_ext_conn_function=gs_ext_conn_function, usr_ext_conn_function=usr_ext_conn_function, sat_ext_conn_function=sat_ext_conn_function,
                                  rate=rate, loss=loss, dT=dT) 
        else:
            for shell in sorted(h5_pos_root.keys()):
                if shell not in h5_del_root:
                    raise RuntimeError(f"Shell '{shell}' exists under /position but not under /delay.")

                h5_pos_shell = h5_pos_root[shell]
                h5_del_shell = h5_del_root[shell]

                # Create corresponding output shell groups
                if shell in h5_pos_root_ext:
                    if overwrite:
                        del h5_pos_root_ext[shell]
                    else:
                        raise RuntimeError(
                            f"Output group {h5_pos_root_ext.name}/{shell} already exists (use --overwrite-gs-groups)."
                        )
                if shell in h5_del_root_ext:
                    if overwrite:
                        del h5_del_root_ext[shell]
                    else:
                        raise RuntimeError(
                            f"Output group {h5_del_root_ext.name}/{shell} already exists (use --overwrite-gs-groups)."
                        )
                if shell in h5_type_root_ext:
                    if overwrite:
                        del h5_type_root_ext[shell]
                    else:
                        raise RuntimeError(
                            f"Output group {h5_type_root_ext.name}/{shell} already exists (use --overwrite-gs-groups)."
                        )

                h5_pos_shell_ext = h5_pos_root_ext.create_group(shell)
                h5_del_shell_ext = h5_del_root_ext.create_group(shell)
                h5_type_shell_ext = h5_type_root_ext.create_group(shell)
                h5_rate_shell_ext = h5_rate_root_ext.create_group(shell)
                h5_loss_shell_ext = h5_loss_root_ext.create_group(shell)
                process_one_shell(shell_name=shell,
                                  GSs=GSs,
                                  USERs=USERs,
                                  SATs=SATs,
                                  h5_pos_root=h5_pos_shell, h5_del_root=h5_del_shell,
                                  h5_pos_root_ext=h5_pos_shell_ext, h5_del_root_ext=h5_del_shell_ext, h5_type_root_ext=h5_type_shell_ext, h5_rate_root_ext=h5_rate_shell_ext, h5_loss_root_ext=h5_loss_shell_ext,
                                  gs_ext_conn_function=gs_ext_conn_function, usr_ext_conn_function=usr_ext_conn_function, sat_ext_conn_function=sat_ext_conn_function,
                                  rate=rate, loss=loss, dT=dT)
## build SatarPerf constellation from XML configuration. Wrote position group in h5 file 
def build_constellation_xml(constellation_name: str, dT: int):
    from src.constellation_generation.by_XML import constellation_configuration
    constellation = constellation_configuration.constellation_configuration(dT=dT,
                                                                            constellation_name=constellation_name)
    print('==============================================')
    print('🛰️ StarPerf Constellation Creation for NetSatBench')
    print('\tDetails of the constellations are as follows :')
    print('\tThe name of the constellation is : ' , constellation.constellation_name)
    print('\tThere are ' , constellation.number_of_shells , ' shell(s) in this constellation')
    print('\tThe information for each shell is as follows:')
    for sh in constellation.shells:
        print('\tshell name : ' , sh.shell_name)
        print('\tshell orbit altitude(km) : ' , sh.altitude)
        print('\tThe shell contains ' , sh.number_of_satellites , ' satellites')
        print('\tThe shell contains ' , sh.number_of_orbits , ' orbits')
        print('\tshell orbital inclination(°) : ' , sh.inclination)
        print('\tshell orbital period (s) : ' , sh.orbit_cycle)
        print('==============================================')
    return constellation

## build SatarPerf constellation object from TLE configuration. Wrote position group in h5 file 
def build_constellation_tle(constellation_name: str, dT: int):
    from src.constellation_generation.by_TLE import constellation_configuration
    constellation = constellation_configuration.constellation_configuration(
        dT=dT,
        constellation_name=constellation_name,
    )
    return constellation

## Run connectivity plugin to determine ISL connectivity and write delay/timeslotN datasets in h5 file. :contentReference[oaicite:14]{index=14}
def run_connectivity(constellation, plugin_name: str, dT: int, mode: str):

    # TLE or XML manager lives under the corresponding tree; try both.
    if mode == "tle":
        from src.TLE_constellation.constellation_connectivity import connectivity_mode_plugin_manager
    elif mode == "xml":
        from src.XML_constellation.constellation_connectivity import connectivity_mode_plugin_manager
    else:
        raise ValueError(f"Unknown mode: {mode}")

    mgr = connectivity_mode_plugin_manager.connectivity_mode_plugin_manager()
    if plugin_name:
        mgr.set_connection_mode(plugin_name)
    mgr.execute_connection_policy(constellation=constellation, dT=dT)

## Verify the output .h5 file has the required groups and datasets as per the interface convention. :contentReference[oaicite:7]{index=7}
def verify_ext_h5(h5_path: Path):
    """
    Verify the output contract required by the PDF:
    - Group name 'delay'
    - Datasets 'timeslot<number>' :contentReference[oaicite:11]{index=11}
    """
    if not h5_path.exists():
        raise FileNotFoundError(f"Expected output file not found: {h5_path}")

    with h5py.File(h5_path, "r") as f:
        groups = ["delay_ext","position_ext","type_ext","rate_ext","loss_ext"]
        for g in groups:
            if g not in f:
                raise RuntimeError(f"{h5_path} has no '{g}' group")
            g_grpup = f[g]
            if not g_grpup.keys():
                raise RuntimeError(f"{h5_path} '{g}' group is empty")
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["xml", "tle"], default="xml",
                    help="Constellation type to build (default: xml). XML requires ./config/*_constellation/*.xml files; TLE requires config/TLE_constellation/*.txt files.")
    ap.add_argument("--constellation-name", default="OneWeb",
                    help="Must match subfolder name in ./config/*_constellation/ (default: OneWeb)")
    ap.add_argument("--dT", type=int, default=15, 
                    help="Timeslot interval in seconds")
    ap.add_argument("--isl-connectivity-plugin", default="positive_Grid",
                    help="Connectivity plugin name for ISL (default is positive_Grid)")
    ap.add_argument("--gs-antenna-plugin", default="void_antenna",
                    help="Antenna plugin name for ground station links (default is void_antenna)")
    ap.add_argument("--user-antenna-plugin", default="pass_antenna",
                    help="Antenna plugin name for user links (default is pass_antenna)")
    ap.add_argument("--isl-rate-plugin", default="pass_rate",
                    help="Rate plugin name for ISL (default is pass_rate)")
    ap.add_argument("--gs-rate-plugin", default="pass_rate",
                    help="Rate plugin name for ground station links (default is pass_rate)")
    ap.add_argument("--user-rate-plugin", default="pass_rate",
                    help="Rate plugin name for user links (default is pass_rate)")
    ap.add_argument("--isl-loss-plugin", default="pass_loss",
                    help="Loss plugin name for ISL (default is pass_loss)")
    ap.add_argument("--gs-loss-plugin", default="pass_loss",
                    help="Loss plugin name for ground station links (default is pass_loss)")
    ap.add_argument("--user-loss-plugin", default="pass_loss",
                    help="Loss plugin name for user links (default is pass_loss)")
    
    ap.add_argument("--isl-rate", default="100mbit", help="Default rate of ISL links (default: 100mbit)")
    ap.add_argument("--gs-rate", default="100mbit", help="Default rate of Sat to Ground Station (Gateway) Links (default: 100mbit)")
    ap.add_argument("--user-rate", default="50mbit", help="Default rate of Sat to User links (default: 50mbit)")
    ap.add_argument("--loss-isl", type=float, default=0.0, help="Default loss rate for ISL links (default: 0.0)")
    ap.add_argument("--loss-gs", type=float, default=0.0, help="Default loss rate for Sat to Ground Station (Gateway) Links (default: 0.0)")
    ap.add_argument("--loss-user", type=float, default=0.0, help="Default loss rate for Sat to User links (default: 0.0)")
    ap.add_argument("--data-root", default="data", 
                    help="StarPerf data directory (default: ./data)")
    ap.add_argument("--include-ground-stations", action="store_true", 
                    help="Append ground stations to the output .h5. requires ./config/ground_stations/<Constellation>.xml")
    ap.add_argument("--include-users", action="store_true", 
                    help="Append users to the output .h5. requires ./config/users/<Constellation>.xml")
    ap.add_argument("--minimum-elevation", type=float, default=25.0,
                    help="Minimum elevation angle (deg) for a satellite to be considered visible from a ground node (GS or USER)")
    ap.add_argument("--overwrite-ext-groups", action="store_true",
                    help="Overwrite NetSatBench extended h5 groups if they already exist.")

    args = ap.parse_args()

    rate = {
        "isl": args.isl_rate,
        "gs": args.gs_rate,
        "user": args.user_rate,
    }
    loss = {
        "isl": args.loss_isl,
        "gs": args.loss_gs,
        "user": args.loss_user,
    }

    antenna_plugins = {
        "gs": args.gs_antenna_plugin,
        "user": args.user_antenna_plugin,
    }
    rate_plugins = {
        "isl": args.isl_rate_plugin,
        "gs": args.gs_rate_plugin,
        "user": args.user_rate_plugin,
    }
    loss_plugins = {
        "isl": args.isl_loss_plugin,
        "gs": args.gs_loss_plugin,
        "user": args.user_loss_plugin,
    }

    # 1) Build constellation
    if args.mode == "xml":
        constellation = build_constellation_xml(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "XML_constellation" / f"{args.constellation_name}.h5"
    else:
        constellation = build_constellation_tle(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "TLE_constellation" / f"{args.constellation_name}.h5"

    # 2) Run ISL connectivity (writes delay/timeslotN into out_h5) :contentReference[oaicite:14]{index=14}
    print(f"🛜 Running StarPerf ISL connectivity plugin '{args.isl_connectivity_plugin}' for constellation '{constellation.constellation_name}'...")
    run_connectivity(constellation, args.isl_connectivity_plugin, args.dT,  args.mode)

    # Ground station XML layout is described in the interface convention.
    if args.include_ground_stations:
        gs_xml = Path("config") / "ground_stations" / f"{args.constellation_name}.xml"
        gs_conn_plugin_path_base = "NetSatBenchKit.ext_connectivity_plugin"
        gs_ext_plugin = {}
        try:
            gs_list = read_ground_stations_xml(gs_xml)
            gs_ext_plugin["antenna"] = importlib.import_module(gs_conn_plugin_path_base + "." + args.gs_antenna_plugin)
            gs_conn_function = getattr(gs_ext_plugin, args.gs_conn_plugin)
        except Exception as e:
            print(f"Error reading ground stations XML: {e} or importing connectivity plugin '{gs_conn_plugin_path}': {e}")
            gs_list = []
    else:
        gs_list = []
    
    if args.include_users:
        usr_xml = Path("config") / "users" / f"{args.constellation_name}.xml"
        usr_conn_plugin_path = f"NetSatBenchKit.ext_connectivity_plugin.{args.user_conn_plugin}"
        try:
            usr_list = read_users_xml(usr_xml)
            usr_connectivity_plugin = importlib.import_module(usr_conn_plugin_path)
            usr_conn_function = getattr(usr_connectivity_plugin, args.user_conn_plugin)
        except Exception as e:
            print(f"Error reading users XML: {e} or importing connectivity plugin '{usr_conn_plugin_path}': {e}")
            usr_list = []
    else:
        usr_list = []
    
    # create SAT object list with consistent indexing as the /position datasets in the .h5 file (i.e. if satellite with id X is at index i in the /position/timeslotN dataset, then SATs[i-1] should be that satellite object since index 0 is reserved/void as per StarPerf convention)
    SATs = []
    for sh in constellation.shells:
        for orbit in sh.orbits:
                for sat in orbit.satellites:
                    SATs.append(sat)

    create_exteded_h5(
        h5_path=out_h5,
        SATs=SATs,
        GSs=gs_list,
        USERs=usr_list,
        min_elevation_deg=args.minimum_elevation,
        sat_conn_function=sat_conn_function,
        gs_conn_function=gs_conn_function if args.include_ground_stations else None,
        usr_conn_function=usr_conn_function if args.include_users else None,
        dT=args.dT,
        rate=rate,
        loss=loss,
        overwrite=args.overwrite_nsb_groups,
    )

    # 3) Verify output
    verify_ext_h5(out_h5)
    print(f"👍 Produced h5 matrix in: {out_h5}")
    print(f"▶️  Proceed with NetSatBenchExport.py to generate sat-config.json and epoch files.")
        
if __name__ == "__main__":
    main()
