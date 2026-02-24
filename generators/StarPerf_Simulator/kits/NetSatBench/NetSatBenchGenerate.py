#!/usr/bin/env python3
"""
StarPerf 2.0 constellation generation with extended h5 data 

Pipeline:
1) Build constellation (XML or TLE)
2) ISL connectivity (positive_Grid by default with StarPerf's internal plugin manager)
3) Add ground stations and users links
4) Extend h5 with delay/rate/loss/type/info datasets for ISL, GS, and user links

Conventions:
- The output extended .h5 file must contain groups: /info /position, /delay, /type, /rate, /loss. Each group should have the same internal structure as the input /position and /delay groups produced by StarPerf, with datasets named timeslot1, timeslot2, etc. corresponding to each timeslot.
- First index are for satellites (starting at 0, followed by ground stations and users in the order they are defined in the input XML files if included.
- The position datasets is a per-shell and per-timeslot dataset with three columns for X/Y/Z ECEF coordinates of the nodes [i]. 
- The delay datasets is a per-shell and per-timeslot square matrix where the value at [i,j] is the delay from node i to node j in seconds, or 0 if no link exists.
- The rate datasets is a per-shell and per-timeslot square matrix where the value at [i,j] is the link rate in Mbps.
- The loss datasets is a per-shell and per-timeslot square matrix where the value at [i,j] is the loss rate as a float between 0 and 1.
- The type datasets is a per-shell vector where the value at [i] is a string indicating the node type ("sat", "gs", or "user").
- The info group contains attributes for metadata such as constellation name, generation timestamp, plugins used, and any other relevant information.
"""

import argparse
from pathlib import Path
import sys
from unicodedata import name

import h5py
import numpy as np
import math
import xml.etree.ElementTree as ET
import sys
# import from upper dir for constellation generation and connectivity plugins
sys.path.append(str(Path(__file__).parent.parent.parent))  # adjust as needed
from kits.NetSatBench.process_one_shell import process_one_shell
import src.XML_constellation.constellation_entity.ground_station as GS
import src.XML_constellation.constellation_entity.satellite as SAT
import re
from typing import List, Dict, Optional
import importlib

# ----------------------- 
# HELPERS
# -----------------------
# extended user class with antenna and GHz information for extended connectivity plugins and interface convention. 
class user:
    def __init__(self , longitude, latitude , name = None , frequency=None , antenna_count = None ,
                 uplink_GHz = None , downlink_GHz = None):
        self.longitude = longitude # the longitude of USER
        self.latitude = latitude # the latitude of USER
        self.name = name  # the description of USER's position
        self.frequency = frequency # the frequency of User, such as Ka,E and so on
        self.antenna_count = antenna_count # the number of antenna of USER
        self.uplink_GHz = uplink_GHz # the uplink GHz of USER
        self.downlink_GHz = downlink_GHz # the downlink GHz of USER

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

def read_users_xml(users_xml_path: Path) -> List[user]:
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
        usr = user(longitude=float(users['USRs']['USR' + str(user_count)]['Longitude']),
                                latitude=float(users['USRs']['USR' + str(user_count)]['Latitude']),
                                name=users['USRs']['USR' + str(user_count)]['Name'])
        if 'Frequency' in users['USRs']['USR' + str(user_count)]:
            usr.frequency = users['USRs']['USR' + str(user_count)]['Frequency']
        if 'Antenna_Count' in users['USRs']['USR' + str(user_count)]:
            usr.antenna_count = int(users['USRs']['USR' + str(user_count)]['Antenna_Count'])
        if 'Uplink_Ghz' in users['USRs']['USR' + str(user_count)]:
            usr.uplink_GHz = float(users['USRs']['USR' + str(user_count)]['Uplink_Ghz'])
        if 'Downlink_Ghz' in users['USRs']['USR' + str(user_count)]:
            usr.downlink_GHz = float(users['USRs']['USR' + str(user_count)]['Downlink_Ghz'])
        USERs.append(usr)
    return USERs

def ext_h5_path_from(base_h5_path: Path) -> Path:
    # OneWeb.h5 -> OneWeb_ext.h5
    return base_h5_path.with_name(base_h5_path.stem + "_ext" + base_h5_path.suffix)

# -----------------------
# MAIN FUNCTION
# -----------------------

def create_exteded_h5(
    h5_path: Path,
    SATs: List[SAT.satellite],
    GSs: List[GS.ground_station],
    USERs: List[user],
    min_elevation_deg: float = 25.0,
    sat_ext_conn_function: Optional[Dict[str, callable]] = None,
    gs_ext_conn_function: Optional[callable] = None,
    usr_ext_conn_function: Optional[callable] = None,
    rate: Optional[dict] = None,
    loss: Optional[dict] = None,
    dT: Optional[int] = 15,
    overwrite: bool = True
) -> None:
    
    if not GSs:
        print("📡 Empty ground station list; check your ground station XML or command line arguments.")
    else:
        print(f"📡 Adding {len(GSs)} ground stations to the .h5 file")
    
    if not USERs:
        print("👤 Empty user list; check your user XML or command line arguments.")
    else:
        print(f"👤 Adding {len(USERs)} users to the .h5 file")
    
    if not SATs:
        print("🛰️ Satellite list is empty; check your StarPerf constellation initialization")
        exit(1)

    base_h5_path = Path(h5_path)
    out_ext_h5_path = ext_h5_path_from(base_h5_path)

    # Create/overwrite the ext file
    ext_mode = "w" if overwrite else "x"

    with h5py.File(base_h5_path, "r") as fin, h5py.File(out_ext_h5_path, ext_mode) as fout:
        if "position" not in fin or "delay" not in fin:
            raise RuntimeError("Expected 'position' and 'delay' groups in the .h5 produced by StarPerf not found.")

        h5_pos_root = fin["position"]
        h5_del_root = fin["delay"]
        
        # Detect layout: if /position contains timeslot datasets -> single-shell, else treat children as shells.
        def _is_timeslot_name(name: str) -> bool:
            return bool(re.match(r"^timeslot\d+$", name))

        pos_children = list(h5_pos_root.keys())
        single_shell = any(_is_timeslot_name(k) for k in pos_children)

        # Create groups in output file, checking for existence if not overwriting
        def _ensure_group_at_root(name: str):
            if name in fout:
                if overwrite:
                    del fout[name]
                    return fout.create_group(name)
                return fout[name]
            return fout.create_group(name)
        
        
        h5_type_root_ext = _ensure_group_at_root("type")
        h5_rate_root_ext = _ensure_group_at_root("rate")
        h5_loss_root_ext = _ensure_group_at_root("loss")
        h5_pos_root_ext = _ensure_group_at_root("position")
        h5_del_root_ext = _ensure_group_at_root("delay")
        
        print(f"🛜 Adding ground and user links with min elevation {min_elevation_deg}°")
        print(f"🎚️ Adding rate, loss and antenna limitation extensions to the .h5 file based on the extended plugins")
        
        if single_shell:
            h5_root_in = {}
            h5_root_in["position"] = h5_pos_root
            h5_root_in["delay"] = h5_del_root
            h5_root_ext = {}
            h5_root_ext["position"] = h5_pos_root_ext
            h5_root_ext["delay"] = h5_del_root_ext
            h5_root_ext["type"] = h5_type_root_ext
            h5_root_ext["rate"] = h5_rate_root_ext
            h5_root_ext["loss"] = h5_loss_root_ext
            process_one_shell(shell_name=None,
                                  GSs=GSs,
                                  USERs=USERs,
                                  SATs=SATs,
                                  min_elevation_deg=min_elevation_deg,
                                  h5_root_in=h5_root_in,
                                  h5_root_ext=h5_root_ext,
                                  gs_ext_conn_function=gs_ext_conn_function, usr_ext_conn_function=usr_ext_conn_function, sat_ext_conn_function=sat_ext_conn_function,
                                  rate=rate, loss=loss, dT=dT, overwrite=overwrite)
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

                h5_root_in = {}
                h5_root_in["position"] = h5_pos_shell
                h5_root_in["delay"] = h5_del_shell

                h5_root_ext = {}
                h5_root_ext["position"] = h5_pos_shell_ext
                h5_root_ext["delay"] = h5_del_shell_ext
                h5_root_ext["type"] = h5_type_shell_ext
                h5_root_ext["rate"] = h5_rate_shell_ext
                h5_root_ext["loss"] = h5_loss_shell_ext

                process_one_shell(shell_name=shell,
                                  GSs=GSs,
                                  USERs=USERs,
                                  SATs=SATs,
                                  min_elevation_deg=min_elevation_deg,
                                  h5_root_in=h5_root_in,h5_root_ext=h5_root_ext, 
                                  gs_ext_conn_function=gs_ext_conn_function, usr_ext_conn_function=usr_ext_conn_function, sat_ext_conn_function=sat_ext_conn_function,
                                  rate=rate, loss=loss, dT=dT, overwrite=overwrite)

## build SatarPerf constellation from XML configuration. Wrote position group in h5 file 
def build_constellation_xml(constellation_name: str, dT: int):
    from src.constellation_generation.by_XML import constellation_configuration
    constellation = constellation_configuration.constellation_configuration(dT=dT,
                                                                            constellation_name=constellation_name)
    print('==============================================')
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
        groups = ["delay","position","type","rate","loss", "info"]
        for g in groups:
            if g not in f:
                raise RuntimeError(f"{h5_path} has no '{g}' group")
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
    ap.add_argument("--gs-antenna-plugin", default="pass_antenna",
                    help="Antenna plugin name for ground station links (default is pass_antenna)")
    ap.add_argument("--user-antenna-plugin", default="pass_antenna",
                    help="Antenna plugin name for user links (default is pass_antenna)")
    ap.add_argument("--gs-antenna-plugin-metadata", default=None,
                    help="File path for ground station antenna plugin metadata (default is None)")
    ap.add_argument("--user-antenna-plugin-metadata", default=None,
                    help="File path for user antenna plugin metadata (default is None)")
    ap.add_argument("--isl-rate-plugin", default="pass_rate",
                    help="Rate plugin name for ISL (default is pass_rate)")
    ap.add_argument("--gs-rate-plugin", default="pass_rate",
                    help="Rate plugin name for ground station links (default is pass_rate)")
    ap.add_argument("--user-rate-plugin", default="pass_rate",
                    help="Rate plugin name for user links (default is pass_rate)")
    ap.add_argument("--isl-rate-plugin-metadata", default=None,
                    help="File path for ISL rate plugin metadata (default is None)")
    ap.add_argument("--gs-rate-plugin-metadata", default=None,
                    help="File path for ground station rate plugin metadata (default is None)")
    ap.add_argument("--user-rate-plugin-metadata", default=None,
                    help="File path for user rate plugin metadata (default is None)")
    ap.add_argument("--isl-loss-plugin", default="pass_loss",
                    help="Loss plugin name for ISL (default is pass_loss)")
    ap.add_argument("--gs-loss-plugin", default="pass_loss",
                    help="Loss plugin name for ground station links (default is pass_loss)")
    ap.add_argument("--user-loss-plugin", default="pass_loss",
                    help="Loss plugin name for user links (default is pass_loss)")
    ap.add_argument("--isl-loss-plugin-metadata", default=None,
                    help="File path for ISL loss plugin metadata (default is None)")
    ap.add_argument("--gs-loss-plugin-metadata", default=None,
                    help="File path for ground station loss plugin metadata (default is None)")
    ap.add_argument("--user-loss-plugin-metadata", default=None,
                    help="File path for user loss plugin metadata (default is None)")
    
    ap.add_argument("--isl-rate", type=float, default=100.0, help="Default rate of ISL links in Mbits (default: 100)")
    ap.add_argument("--gs-rate", type=float, default=100.0, help="Default rate of Sat to Ground Station (Gateway) Links in Mbits (default: 100)")
    ap.add_argument("--user-rate", type=float, default=50.0, help="Default rate of Sat to User links in Mbits (default: 50)")
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
    ap.add_argument("--overwrite", action="store_true",
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
    print(f"🛰️ StarPerf Constellation Creation for NetSatBench with constellation '{args.constellation_name}' in mode '{args.mode}' with dT={args.dT}s")
    
    # 1) Build constellation
    if args.mode == "xml":
        constellation = build_constellation_xml(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "XML_constellation" / f"{args.constellation_name}.h5"
    else:
        constellation = build_constellation_tle(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "TLE_constellation" / f"{args.constellation_name}.h5"

    # 2) Run ISL connectivity (writes delay/timeslotN into out_h5) :contentReference[oaicite:14]{index=14}
    print(f"🛜 Adding ISL links with StarPerf plugin {args.isl_connectivity_plugin} ")
    run_connectivity(constellation, args.isl_connectivity_plugin, args.dT,  args.mode)

    # Ground station XML layout is described in the interface convention.
    if args.include_ground_stations:
        gs_xml = Path("config") / "ground_stations" / f"{args.constellation_name}.xml"
        gs_conn_plugin_path_base = "kits.NetSatBench.ext_connectivity_plugin"
        gs_ext_conn_function = {}
        try:
            gs_list = read_ground_stations_xml(gs_xml)
            gs_ext_plugin_antenna = importlib.import_module(gs_conn_plugin_path_base + "." + args.gs_antenna_plugin)
            gs_ext_plugin_rate = importlib.import_module(gs_conn_plugin_path_base + "." + args.gs_rate_plugin)
            gs_ext_plugin_loss = importlib.import_module(gs_conn_plugin_path_base + "." + args.gs_loss_plugin)
            gs_ext_conn_function["antenna"] = getattr(gs_ext_plugin_antenna, args.gs_antenna_plugin)
            gs_ext_conn_function["rate"] = getattr(gs_ext_plugin_rate, args.gs_rate_plugin)
            gs_ext_conn_function["loss"] = getattr(gs_ext_plugin_loss, args.gs_loss_plugin)
            # test metadata loading for GS plugins even if not used since they are part of the interface convention
            if args.gs_antenna_plugin_metadata is not None:
                with open(args.gs_antenna_plugin_metadata, "r") as f:
                    pass
            if args.gs_rate_plugin_metadata is not None:
                with open(args.gs_rate_plugin_metadata, "r") as f:
                    pass
            if args.gs_loss_plugin_metadata is not None:
                with open(args.gs_loss_plugin_metadata, "r") as f:
                    pass
            gs_ext_conn_function["antenna_metadata"] = args.gs_antenna_plugin_metadata
            gs_ext_conn_function["rate_metadata"] = args.gs_rate_plugin_metadata
            gs_ext_conn_function["loss_metadata"] = args.gs_loss_plugin_metadata
        except Exception as e:
            print(f"❌ Error reading ground stations XML: {e} or importing connectivity plugins in '{gs_conn_plugin_path_base}': {e}")
            gs_list = []
            gs_ext_conn_function = {}
    else:
        gs_list = []
    
    if args.include_users:
        usr_xml = Path("config") / "users" / f"{args.constellation_name}.xml"
        usr_conn_plugin_path_base = "kits.NetSatBench.ext_connectivity_plugin"
        usr_ext_conn_function = {}
        try:
            usr_list = read_users_xml(usr_xml)
            usr_ext_plugin_antenna = importlib.import_module(usr_conn_plugin_path_base + "." + args.user_antenna_plugin)
            usr_ext_plugin_rate = importlib.import_module(usr_conn_plugin_path_base + "." + args.user_rate_plugin)
            usr_ext_plugin_loss = importlib.import_module(usr_conn_plugin_path_base + "." + args.user_loss_plugin)
            usr_ext_conn_function["antenna"] = getattr(usr_ext_plugin_antenna, args.user_antenna_plugin)
            usr_ext_conn_function["rate"] = getattr(usr_ext_plugin_rate, args.user_rate_plugin)
            usr_ext_conn_function["loss"] = getattr(usr_ext_plugin_loss, args.user_loss_plugin)

            # test metadata loading for user plugins even if not used since they are part of the interface convention
            if args.user_antenna_plugin_metadata is not None:
                with open(args.user_antenna_plugin_metadata, "r") as f:
                    pass
            if args.user_rate_plugin_metadata is not None:
                with open(args.user_rate_plugin_metadata, "r") as f:
                    pass
            if args.user_loss_plugin_metadata is not None:
                with open(args.user_loss_plugin_metadata, "r") as f:
                    pass
            usr_ext_conn_function["antenna_metadata"] = args.user_antenna_plugin_metadata
            usr_ext_conn_function["rate_metadata"] = args.user_rate_plugin_metadata
            usr_ext_conn_function["loss_metadata"] = args.user_loss_plugin_metadata
        except Exception as e:
            print(f"❌ Error reading users XML: {e} or importing connectivity plugins in '{usr_conn_plugin_path_base}': {e}")
            usr_list = []
            usr_ext_conn_function = {}
    else:
        usr_list = []
    
    # add extended conn plugin functions for ISL
    sat_conn_plugin_path_base = "kits.NetSatBench.ext_connectivity_plugin"
    sat_ext_conn_function = {}
    try:
        sat_ext_plugin_rate = importlib.import_module(sat_conn_plugin_path_base + "." + args.isl_rate_plugin)
        sat_ext_plugin_loss = importlib.import_module(sat_conn_plugin_path_base + "." + args.isl_loss_plugin)
        sat_ext_conn_function["rate"] = getattr(sat_ext_plugin_rate, args.isl_rate_plugin)
        sat_ext_conn_function["loss"] = getattr(sat_ext_plugin_loss, args.isl_loss_plugin)
        # test metadata loading for ISL plugins even if not used since they are part of the interface convention
        if args.isl_rate_plugin_metadata is not None:
            with open(args.isl_rate_plugin_metadata, "r") as f:
                pass
        if args.isl_loss_plugin_metadata is not None:
            with open(args.isl_loss_plugin_metadata, "r") as f:
                pass
        sat_ext_conn_function["rate_metadata"] = args.isl_rate_plugin_metadata
        sat_ext_conn_function["loss_metadata"] = args.isl_loss_plugin_metadata
    except Exception as e:
        print(f"❌ Error importing ISL connectivity plugins in '{sat_conn_plugin_path_base}': {e}")
        sat_ext_conn_function = {}
    
    # create SAT object list with consistent indexing as the /position datasets in the .h5 file (i.e. if satellite with id X is at index i in the /position/timeslotN dataset, then SATs[i-1] should be that satellite object since index 0 is reserved/void as per StarPerf convention)
    SATs = []
    for sh in constellation.shells:
        for orbit in sh.orbits:
                for sat in orbit.satellites:
                    SATs.append(sat)

    create_exteded_h5(
        h5_path=out_h5,
        SATs=SATs,
        GSs=gs_list if args.include_ground_stations else [],
        USERs=usr_list if args.include_users else [],
        min_elevation_deg=args.minimum_elevation,
        sat_ext_conn_function=sat_ext_conn_function,
        gs_ext_conn_function=gs_ext_conn_function if args.include_ground_stations else {},
        usr_ext_conn_function=usr_ext_conn_function if args.include_users else {},
        dT=args.dT,
        rate=rate,
        loss=loss,
        overwrite=args.overwrite,
    )

    # store h5 metadata about the generation in info group attributes
    with h5py.File(ext_h5_path_from(base_h5_path=out_h5), "a") as f:
        if "info" in f:
            if args.overwrite:
                del f["info"]
            else:
                raise RuntimeError(f"Output group {f.name}/info already exists (use --overwrite).")
        h5_info_root_ext = f.create_group("info")
        h5_info_root_ext.attrs["constellation_name"] = constellation.constellation_name
        h5_info_root_ext.attrs["generation_mode"] = args.mode
        h5_info_root_ext.attrs["generation_timestamp"] = str(np.datetime64("now"))
        h5_info_root_ext.attrs["dT"] = args.dT
        h5_info_root_ext.attrs["num_satellites"] = len(SATs)
        h5_info_root_ext.attrs["num_ground_stations"] = len(gs_list) if args.include_ground_stations else 0
        h5_info_root_ext.attrs["num_users"] = len(usr_list) if args.include_users else 0
        h5_info_root_ext.attrs["minimum_elevation_deg"] = args.minimum_elevation
        h5_info_root_ext.attrs["rate_isl_Mbps"] = args.isl_rate
        h5_info_root_ext.attrs["rate_gs_Mbps"] = args.gs_rate
        h5_info_root_ext.attrs["rate_user_Mbps"] = args.user_rate
        h5_info_root_ext.attrs["loss_isl"] = args.loss_isl
        h5_info_root_ext.attrs["loss_gs"] = args.loss_gs
        h5_info_root_ext.attrs["loss_user"] = args.loss_user
        h5_info_root_ext.attrs["isl_connectivity_plugin"] = args.isl_connectivity_plugin
        h5_info_root_ext.attrs["isl_rate_plugin"] = args.isl_rate_plugin
        if args.isl_rate_plugin_metadata is not None:
            h5_info_root_ext.attrs["isl_rate_plugin_metadata"] = args.isl_rate_plugin_metadata
        h5_info_root_ext.attrs["isl_loss_plugin"] = args.isl_loss_plugin
        if args.isl_loss_plugin_metadata is not None:
            h5_info_root_ext.attrs["isl_loss_plugin_metadata"] = args.isl_loss_plugin_metadata

        if args.include_ground_stations:
            h5_info_root_ext.attrs["gs_antenna_plugin"] = args.gs_antenna_plugin
            if args.gs_antenna_plugin_metadata is not None:
                h5_info_root_ext.attrs["gs_antenna_plugin_metadata"] = args.gs_antenna_plugin_metadata
            h5_info_root_ext.attrs["gs_rate_plugin"] = args.gs_rate_plugin
            if args.gs_rate_plugin_metadata is not None:
                h5_info_root_ext.attrs["gs_rate_plugin_metadata"] = args.gs_rate_plugin_metadata
            h5_info_root_ext.attrs["gs_loss_plugin"] = args.gs_loss_plugin
            if args.gs_loss_plugin_metadata is not None:
                h5_info_root_ext.attrs["gs_loss_plugin_metadata"] = args.gs_loss_plugin_metadata
        if args.include_users:
            h5_info_root_ext.attrs["user_antenna_plugin"] = args.user_antenna_plugin
            if args.user_antenna_plugin_metadata is not None:
                h5_info_root_ext.attrs["user_antenna_plugin_metadata"] = args.user_antenna_plugin_metadata
            h5_info_root_ext.attrs["user_rate_plugin"] = args.user_rate_plugin
            if args.user_rate_plugin_metadata is not None:
                h5_info_root_ext.attrs["user_rate_plugin_metadata"] = args.user_rate_plugin_metadata
            h5_info_root_ext.attrs["user_loss_plugin"] = args.user_loss_plugin
            if args.user_loss_plugin_metadata is not None:
                h5_info_root_ext.attrs["user_loss_plugin_metadata"] = args.user_loss_plugin_metadata

       
    # 3) Verify output
    verify_ext_h5(ext_h5_path_from(base_h5_path=out_h5))
    print(f"👍 Produced extended h5 matrix in: {ext_h5_path_from(base_h5_path=out_h5)}")
    print(f"▶️  Proceed with NetSatBenchExport.py to generate sat-config.json and epoch files.")
        
if __name__ == "__main__":
    main()
