#!/usr/bin/env python3
"""
StarPerf 2.0 minimal simulation driver according to interface_convention.pdf.

Pipeline:
1) Build constellation (XML or TLE)
2) (Optional) beam placement (bent-pipe / GSL-related)
3) ISL connectivity (positive_Grid by default)
4) Verify HDF5 output contains delay/timeslot*

This follows the framework+plugin execution mechanism described in the PDF:
- StarPerf.py is the official entry, but each module can be called independently. :contentReference[oaicite:5]{index=5}
- Connectivity plugins are managed by connectivity_mode_plugin_manager and default to positive_Grid. :contentReference[oaicite:6]{index=6}
- After ISL establishment, plugin must write delay matrices to data/*_constellation/*.h5 under group 'delay' with datasets 'timeslotN'. :contentReference[oaicite:7]{index=7}
"""

import argparse
from pathlib import Path

import h5py
import numpy as np
import math
import xml.etree.ElementTree as ET
import src.XML_constellation.constellation_entity.ground_station as GS
import src.XML_constellation.constellation_entity.satellite as SAT
import src.XML_constellation.constellation_entity.user as USER
import re
from typing import List, Tuple, Dict, Optional

# Read xml document
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

# Read xml document
def read_xml_file(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    return {root.tag: xml_to_dict(root)}

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

def judgePointToSatellite(sat_x , sat_y , sat_z , point_x , point_y , point_z , minimum_elevation):
    A = 1.0 * point_x * (point_x - sat_x) + point_y * (point_y - sat_y) + point_z * (point_z - sat_z)
    B = 1.0 * math.sqrt(point_x * point_x + point_y * point_y + point_z * point_z)
    C = 1.0 * math.sqrt(math.pow(sat_x - point_x, 2) + math.pow(sat_y - point_y, 2) + math.pow(sat_z - point_z, 2))
    angle = math.degrees(math.acos(A / (B * C))) # calculate angles and convert radians to degrees
    if angle < 90 + minimum_elevation or math.fabs(angle - 90 - minimum_elevation) <= 1e-6:
        return False
    else:
        return True

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

def add_nsb_extension_to_h5(
    h5_path: Path,
    GSs: List[GS.ground_station],
    USERs: List[USER.user],
    min_elevation_deg: float,
    out_position_group: str = "position_nsb",
    out_delay_group: str = "delay_nsb",
    overwrite: bool = False,
) -> None:
    
    if not GSs:
        print("📡 Empty ground station list; check your ground station XML or command line arguments.")
    else:
        print(f"📡 Adding {len(GSs)} ground stations")
    
    if not USERs:
        print("👤 Empty user list; check your user XML or command line arguments.")
    else:
        print(f"👤 Adding {len(USERs)} users")

    with h5py.File(h5_path, "a") as f:
        if "position" not in f or "delay" not in f:
            raise RuntimeError("Expected 'position' and 'delay' groups in the .h5 produced by StarPerf.")

        pos_root = f["position"]
        dly_root = f["delay"]
        n_gs = len(GSs)
        gs_pos = np.array([(0.0, 0.0, 0.0)] * n_gs)  # placeholder for GS positions in (longitude, latitude, altitude)
        gs_pos_ecef = np.array([(0.0, 0.0, 0.0)] * n_gs)  # placeholder for GS positions in ECEF
        n_usrs = len(USERs)
        usr_pos = np.array([(0.0, 0.0, 0.0)] * n_usrs)  # placeholder for USER positions in (longitude, latitude, altitude) 
        usr_pos_ecef = np.array([(0.0, 0.0, 0.0)] * n_usrs)  # placeholder for USER positions in ECEF

        for i, gs in enumerate(GSs):
            gs.altitude = 0.0  # Assuming GS altitude is 0 for simplicity; adjust if your XML includes altitude
            gs_pos[i,:] = (gs.longitude, gs.latitude, gs.altitude)  # fill in (lon, lat, alt)
            gs_pos_ecef[i,:] = latilong_to_descartes(gs)  # convert to (x,y,z) in ECEF

        for i, usr in enumerate(USERs):
            usr.altitude = 0.0  # Assuming USER altitude is 0 for simplicity; adjust if your XML includes altitude
            usr_pos[i,:] = (usr.longitude, usr.latitude, usr.altitude)  # fill in (lon, lat, alt)
            usr_pos_ecef[i,:] = latilong_to_descartes(usr)  # convert to (x,y,z) in ECEF
        
        # Detect layout: if /position contains timeslot datasets -> single-shell, else treat children as shells.
        def _is_timeslot_name(name: str) -> bool:
            return bool(re.match(r"^timeslot\d+$", name))

        pos_children = list(pos_root.keys())
        single_shell = any(_is_timeslot_name(k) for k in pos_children)

        # Create / reuse output groups at root
        def _ensure_group_at_root(name: str):
            if name in f:
                if overwrite:
                    del f[name]
                    return f.create_group(name)
                return f[name]
            return f.create_group(name)
        out_type_root = _ensure_group_at_root("type_nsb")
        out_pos_root = _ensure_group_at_root(out_position_group)
        out_dly_root = _ensure_group_at_root(out_delay_group)

        def process_one_shell(shell_name: Optional[str], pos_g, dly_g, out_pos_g, out_dly_g, out_type_g, gs_pos, gs_pos_ecef, usrs_pos, usrs_pos_ecef):
            # Discover timeslots
            timeslots = sorted(
                pos_g.keys(),
                key=lambda s: int("".join(ch for ch in s if ch.isdigit()) or "0"),
            )
            if not timeslots:
                raise RuntimeError(f"No timeslot datasets under /position/{shell_name or ''}.")

            # Satellite count from first timeslot
            first_pos = pos_g[timeslots[0]][:]
            n_sat = int(first_pos.shape[0])
            n_gs = int(gs_pos.shape[0])
            n_usrs = int(usrs_pos.shape[0])
            n_tot = n_sat + n_gs + n_usrs

            for ts in timeslots:
                sat_pos = pos_g[ts][:]        # (n_sat, 3) longitude, latitude, altitude
                sat_sat_delay = dly_g[ts][:,:]  # (n_sat+1, n_sat+1) #  first row/col left void by StarPerf

                if n_gs > 0:
                    pos_ext = np.vstack([sat_pos.astype("float64", copy=False), gs_pos])
                else:
                    pos_ext = sat_pos.astype("float64", copy=False)
                
                if n_usrs > 0:
                    pos_ext =  np.vstack([pos_ext, usrs_pos])

                # Extended delay: copy sat-sat then fill sat-gs
                d_ext = np.zeros((n_tot+1, n_tot+1), dtype="float64")
                d_ext[:n_sat+1, :n_sat+1] = sat_sat_delay

                for gi, gsp_ecef in enumerate(gs_pos_ecef):
                    gidx = n_sat + 1 + gi
                    for si, satp in enumerate(sat_pos):
                        sidx = si + 1
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           gsp_ecef[0] , gsp_ecef[1] , gsp_ecef[2] ,
                                                           min_elevation_deg)
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - gsp_ecef[0]) ** 2
                                + (spos_ecef[1] - gsp_ecef[1]) ** 2
                                + (spos_ecef[2] - gsp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            d_ext[sidx, gidx] = delay_s
                            d_ext[gidx, sidx] = delay_s 
                
                for ui, usp_ecef in enumerate(usrs_pos_ecef):
                    uidx = n_sat + n_gs + 1 + ui
                    for si, satp in enumerate(sat_pos):
                        sidx = si + 1
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           usp_ecef[0] , usp_ecef[1] , usp_ecef[2] ,
                                                           min_elevation_deg)
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - usp_ecef[0]) ** 2
                                + (spos_ecef[1] - usp_ecef[1]) ** 2
                                + (spos_ecef[2] - usp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            d_ext[sidx, uidx] = delay_s
                            d_ext[uidx, sidx] = delay_s 
                # Write datasets
                if ts in out_pos_g:
                    if overwrite:
                        del out_pos_g[ts]
                    else:
                        raise RuntimeError(
                            f"Dataset {out_pos_g.name}/{ts} already exists (use --overwrite-gs-groups)."
                        )
                if ts in out_dly_g:
                    if overwrite:
                        del out_dly_g[ts]
                    else:
                        raise RuntimeError(
                            f"Dataset {out_dly_g.name}/{ts} already exists (use --overwrite-gs-groups)."
                        )
                out_dly_g.create_dataset(ts, data=d_ext, compression="gzip", compression_opts=4)
                out_pos_g.create_dataset(ts,data=pos_ext, compression="gzip", compression_opts=4)              
            
            # Write type_nsb dataset
            type_nsb = np.array([b"satellite"] * n_tot, dtype='S')
            for gi in range(n_gs):
                type_nsb[n_sat + gi] = b"gateway"
            for ui in range(n_usrs):
                type_nsb[n_sat + n_gs + ui] = b"user"
            out_type_g.create_dataset("type_nsb", data=type_nsb, compression="gzip", compression_opts=4)
            
        if single_shell:
            # Input: /position/timeslot*, /delay/timeslot*
            process_one_shell(None, pos_root, dly_root, out_pos_root, out_dly_root, out_type_root, gs_pos, gs_pos_ecef, usr_pos, usr_pos_ecef)
        else:
            # Input: /position/shellX/timeslot*, /delay/shellX/timeslot*
            for shell in sorted(pos_root.keys()):
                if shell not in dly_root:
                    raise RuntimeError(f"Shell '{shell}' exists under /position but not under /delay.")

                pos_g = pos_root[shell]
                dly_g = dly_root[shell]

                # Create corresponding output shell groups
                if shell in out_pos_root:
                    if overwrite:
                        del out_pos_root[shell]
                    else:
                        raise RuntimeError(
                            f"Output group {out_position_group}/{shell} already exists (use --overwrite-gs-groups)."
                        )
                if shell in out_dly_root:
                    if overwrite:
                        del out_dly_root[shell]
                    else:
                        raise RuntimeError(
                            f"Output group {out_delay_group}/{shell} already exists (use --overwrite-gs-groups)."
                        )
                if shell in out_type_root:
                    if overwrite:
                        del out_type_root[shell]
                    else:
                        raise RuntimeError(
                            f"Output group type_nsb/{shell} already exists (use --overwrite-gs-groups)."
                        )

                out_pos_shell = out_pos_root.create_group(shell)
                out_dly_shell = out_dly_root.create_group(shell)
                out_type_shell = out_type_root.create_group(shell)
                process_one_shell(shell, pos_g, dly_g, out_pos_shell, out_dly_shell, out_type_shell, gs_pos, gs_pos_ecef, usr_pos, usr_pos_ecef)




def build_constellation_xml(constellation_name: str, dT: int):
    """
    XML constellation generation: the PDF describes calling a constellation_configuration
    function to build the constellation. :contentReference[oaicite:8]{index=8}

    NOTE: exact import path depends on repo; adjust if needed.
    """
    from src.constellation_generation.by_XML import constellation_configuration
    
    # generate the constellations
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


def build_constellation_tle(constellation_name: str, dT: int):
    """
    TLE constellation generation is described as a 5-stage pipeline in the PDF. :contentReference[oaicite:9]{index=9}
    If your repo exposes a single 'constellation_configuration' wrapper for TLE as well,
    you can use it; otherwise you must run the stages (download_TLE_data, mapping, positions, etc.).
    """
    from src.constellation_generation.by_TLE import constellation_configuration
    constellation = constellation_configuration.constellation_configuration(
        dT=dT,
        constellation_name=constellation_name,
    )
    return constellation


def run_connectivity(constellation, plugin_name: str, dT: int, mode: str):
    """
    Connectivity execution mechanism per PDF:
    - instantiate connectivity_mode_plugin_manager
    - optionally set_connection_mode(plugin_name)
    - execute_connection_policy(constellation, dT)
    Default mode after init is 'positive_Grid'. :contentReference[oaicite:10]{index=10}
    """
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


def verify_delay_h5(h5_path: Path):
    """
    Verify the output contract required by the PDF:
    - Group name 'delay'
    - Datasets 'timeslot<number>' :contentReference[oaicite:11]{index=11}
    """
    if not h5_path.exists():
        raise FileNotFoundError(f"Expected output file not found: {h5_path}")

    with h5py.File(h5_path, "r") as f:
        groups = ["delay_nsb","position_nsb","type_nsb"]
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
                    help="Constellation type to build (XML_constellation or TLE_constellation)")
    ap.add_argument("--constellation-name", default="OneWeb",
                    help="Must match subfolder name in config/*_constellation/ (e.g., 'OneWeb' or 'Telesat')")
    ap.add_argument("--dT", type=int, default=15, help="Timeslot interval in seconds")
    ap.add_argument("--connectivity-plugin", default="positive_Grid",
                    help="Connectivity plugin name (default is positive_Grid)")
    ap.add_argument("--data-root", default="data", help="StarPerf data directory (default: ./data)")
    ap.add_argument("--include-ground-stations", action="store_true",
                    help="Append ground stations to the output .h5.")
    ap.add_argument("--ground-station-xml", default=None,
                    help="Path to config/ground_stations/<Constellation>.xml. "
                         "If omitted, defaults to config/ground_stations/<constellation-name>.xml.")
    ap.add_argument("--include-users", action="store_true",
                    help="Append users to the output .h5.")
    ap.add_argument("--user-xml", default=None,
                    help="Path to config/users/<Constellation>.xml. "
                         "If omitted, defaults to config/users/<constellation-name>.xml.")
    ap.add_argument("--minimum-elevation", type=float, default=25.0,
                    help="Minimum elevation angle (deg) for a satellite to be considered visible from a GS.")
    ap.add_argument("--overwrite-nsb-groups", action="store_true",
                    help="Overwrite NetSatBench h5 groups if they already exist.")

    args = ap.parse_args()

    # 1) Build constellation
    if args.mode == "xml":
        constellation = build_constellation_xml(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "XML_constellation" / f"{args.constellation_name}.h5"
    else:
        constellation = build_constellation_tle(args.constellation_name, args.dT)
        out_h5 = Path(args.data_root) / "TLE_constellation" / f"{args.constellation_name}.h5"

    # 2) Run ISL connectivity (writes delay/timeslotN into out_h5) :contentReference[oaicite:14]{index=14}
    print(f"🔌 Running ISL connectivity plugin '{args.connectivity_plugin}' for constellation '{constellation.constellation_name}'...")
    run_connectivity(constellation, args.connectivity_plugin, args.dT,  args.mode)

    # 4) Optionally append ground stations to delay/position matrices.
    # Ground station XML layout is described in the interface convention. fileciteturn4file10
    if args.include_ground_stations:
        if args.ground_station_xml is None:
            gs_xml = Path("config") / "ground_stations" / f"{args.constellation_name}.xml"
        else:
            gs_xml = Path(args.ground_station_xml)
        gs_list = read_ground_stations_xml(gs_xml)
    else:
        gs_list = []
    
    if args.include_users:
        if args.user_xml is None:
            usr_xml = Path("config") / "users" / f"{args.constellation_name}.xml"
        else:
            usr_xml = Path(args.user_xml)
        usr_list = read_users_xml(usr_xml)
    else:
        usr_list = []

    add_nsb_extension_to_h5(
        h5_path=out_h5,
        GSs=gs_list,
        USERs=usr_list,
        min_elevation_deg=args.minimum_elevation,
        out_position_group="position_nsb",
        out_delay_group="delay_nsb",
        overwrite=args.overwrite_nsb_groups,
    )

    # 3) Verify output
    verify_delay_h5(out_h5)
    print(f"👍 Produced matrices in: {out_h5}")

        
if __name__ == "__main__":
    main()
