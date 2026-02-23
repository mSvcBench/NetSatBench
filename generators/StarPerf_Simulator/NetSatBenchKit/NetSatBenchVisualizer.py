#!/usr/bin/env python3
"""
Generates Cesium HTML files including Satellites, Ground Stations, and Users with dynamic coverage radius.
"""

import argparse
import os
import math
import xml.etree.ElementTree as ET
from pathlib import Path
import sys

# Append StarPerf root to path to safely import its modules without modifying them
sys.path.append(str(Path(__file__).parent.parent))

# Import necessary functions from StarPerf's visualization module
from visualization.constellation_visualization import get_satellites_list, add_coverage_circle , get_ISL

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

def read_xml_file(file_path: str) -> dict:
    if not os.path.exists(file_path):
        return {}
    tree = ET.parse(file_path)
    root = tree.getroot()
    return {root.tag: xml_to_dict(root)}

def calculate_dynamic_coverage_radius(altitude_km: float, min_elevation_deg: float) -> float:
    """
    Calculate the coverage radius of a satellite based on its altitude and minimum elevation angle.
    :param altitude_km: Altitude of the satellite in kilometers
    :param min_elevation_deg: Minimum elevation angle in degrees
    :return: Coverage radius in meters
    """
    Re = 6371.0  # Earth radius in kilometers
    epsilon = math.radians(min_elevation_deg)
    
    arg = (Re / (Re + altitude_km)) * math.cos(epsilon)
    if arg > 1.0 or arg < -1.0:
        return 0.0
            
    gamma = math.acos(arg) - epsilon
    coverage_radius = Re * gamma
    
    return coverage_radius * 1000.0  # Convert to meters

def add_ground_stations(constellation_name: str, color: str = "GREEN", radius: float = 30000.0) -> str:
    """
    Generate Cesium JavaScript code for Ground Stations based on constellation XML.
    """
    content_string = ""
    xml_path = f"./config/ground_stations/{constellation_name}.xml"
    data = read_xml_file(xml_path)
    
    for gs_key, gs_info in data.get("GSs", {}).items():
        if isinstance(gs_info, dict):
            lat = gs_info.get("Latitude", "0")
            lon = gs_info.get("Longitude", "0")
            name = gs_info.get("Description", gs_key)

            content_string += (
                f"viewer.entities.add({{name : 'GS: {name}', "
                f"position: Cesium.Cartesian3.fromDegrees({lon}, {lat}, 0), "
                f"ellipsoid : {{radii : new Cesium.Cartesian3({radius}, {radius}, {radius}), "
                f"material : Cesium.Color.{color}.withAlpha(1)}}}});\n"
            )
    return content_string

def add_user_terminals(constellation_name: str, color: str = "YELLOW", radius: float = 30000.0) -> str:
    """
    Generate Cesium JavaScript code for User Terminals based on constellation XML.
    """
    content_string = ""
    xml_path = f"./config/users/{constellation_name}.xml"
    data = read_xml_file(xml_path)
    
    for usr_key, usr_info in data.get("USRs", {}).items():
        if isinstance(usr_info, dict):
            lat = usr_info.get("Latitude", "0")
            lon = usr_info.get("Longitude", "0")
            name = usr_info.get("Description", usr_key)

            content_string += (
                f"viewer.entities.add({{name : 'USR: {name}', "
                f"position: Cesium.Cartesian3.fromDegrees({lon}, {lat}, 0), "
                f"ellipsoid : {{radii : new Cesium.Cartesian3({radius}, {radius}, {radius}), "
                f"material : Cesium.Color.{color}.withAlpha(1)}}}});\n"
            )
    return content_string

def generate_extended_visualization(constellation_name: str, min_elevation_deg: float, outdir: str, sat_color: str , gs_color: str, user_color: str, with_ISL: bool):
    """
    Core function to read constellation data and build the final HTML independently.
    """
    xml_file_path = f"./config/XML_constellation/{constellation_name}.xml"
    head_html_file = "./visualization/html_head_tail/head.html"
    tail_html_file = "./visualization/html_head_tail/tail.html"
    
    data = read_xml_file(xml_file_path)
    if not data:
        print(f"❌ Constellation file {xml_file_path} not found.")
        sys.exit(1)
        
    num_shells = int(data['constellation']['number_of_shells'])
    constellation_info = []
    
    for count in range(1, num_shells + 1):
        shell_data = data['constellation'][f'shell{count}']
        altitude = int(shell_data['altitude'])
        orbit_cycle = int(shell_data['orbit_cycle'])
        inclination = float(shell_data['inclination'])
        num_orbit = int(shell_data['number_of_orbit'])
        num_sat_per_orbit = int(shell_data['number_of_satellite_per_orbit'])
        
        mean_motion_rev_per_day = 1.0 * 86400 / orbit_cycle
        constellation_info.append([mean_motion_rev_per_day, altitude, num_orbit, num_sat_per_orbit, inclination])

    content_string = ""
    for shell in constellation_info:
        num_orbit = shell[2]
        num_sat_per_orbit = shell[3]
        satellites = get_satellites_list(shell[0], shell[1], num_orbit, num_sat_per_orbit, shell[4])
        
        #  Add satellites with dynamic coverage circles
        for j in range(len(satellites)):
            satellites[j]["satellite"].compute("1949-10-01 00:00:00")
            sublong = math.degrees(satellites[j]["satellite"].sublong)
            sublat = math.degrees(satellites[j]["satellite"].sublat)
            alt_m = satellites[j]["altitude"] * 1000
         
            content_string += (
                f"var redSphere = viewer.entities.add({{name : '', "
                f"position: Cesium.Cartesian3.fromDegrees({sublong}, {sublat}, {alt_m}), "
                f"ellipsoid : {{radii : new Cesium.Cartesian3(30000.0, 30000.0, 30000.0), "
                f"material : Cesium.Color.{sat_color}.withAlpha(1)}}}});\n"
            )
            
          #  Add coverage circles if ISL is not included, otherwise just add satellite dots
            if not with_ISL:
                dynamic_radius = calculate_dynamic_coverage_radius(satellites[j]["altitude"], min_elevation_deg)
                content_string += add_coverage_circle(satellites[j]["satellite"], dynamic_radius, sat_color)
        # If ISL is included, we will only add satellite dots and visualize ISL links without coverage circles to avoid cluttering the visualization.
        if with_ISL:
            orbit_links = get_ISL(satellites, num_orbit, num_sat_per_orbit)

            for key in orbit_links:
                sat1 = orbit_links[key]["sat1"]
                sat2 = orbit_links[key]["sat2"]
                
                satellites[sat1]["satellite"].compute("1949-10-01 00:00:00")
                satellites[sat2]["satellite"].compute("1949-10-01 00:00:00")
                                
                lon1 = math.degrees(satellites[sat1]["satellite"].sublong)
                lat1 = math.degrees(satellites[sat1]["satellite"].sublat)
                alt1 = satellites[sat1]["altitude"] * 1000
                
                lon2 = math.degrees(satellites[sat2]["satellite"].sublong)
                lat2 = math.degrees(satellites[sat2]["satellite"].sublat)
                alt2 = satellites[sat2]["altitude"] * 1000
                
                content_string += (
                    f"viewer.entities.add({{name : 'ISL', polyline: {{ positions: Cesium.Cartesian3.fromDegreesArrayHeights(["
                    f"{lon1}, {lat1}, {alt1}, {lon2}, {lat2}, {alt2}"
                    f"]), width: 1.5, arcType: Cesium.ArcType.NONE, "
                    f"material: Cesium.Color.{sat_color}.withAlpha(0.3)}}}});\n"
                )

    # Append GS and Users using the custom functions
    content_string += add_ground_stations(constellation_name, color=gs_color)
    content_string += add_user_terminals(constellation_name, color=user_color)

    # Write to final HTML
    os.makedirs(outdir, exist_ok=True)
    isl_suffix = "_with_ISL" if with_ISL else "_without_ISL"
    out_path = os.path.join(outdir, f"{constellation_name}_NetSatBench{isl_suffix}.html")
    
    with open(out_path, 'w') as writer, open(head_html_file, 'r') as head, open(tail_html_file, 'r') as tail:
        writer.write(head.read())
        writer.write(content_string)
        writer.write(tail.read())
        
    print(f"✅ Visualization successfully generated at: {out_path}")

def main():
    ap = argparse.ArgumentParser(description="NetSatBench Extended Visualization Generator")
    ap.add_argument("--constellation-name", required=True, help="Name of the constellation (e.g., OneWeb, Starlink)")
    ap.add_argument("--minimum-elevation", type=float, default=25.0, help="Minimum elevation angle in degrees used for connectiong satellites with ground stations and users (default: 25.0)")
    ap.add_argument("--outdir", default="./visualization/CesiumAPP", help="Output directory for the HTML file")
    ap.add_argument("--satellite-color", default="RED", help="Color of the satellite dot and footprint (default:RED)")
    ap.add_argument("--user-color", default="YELLOW", help="Color of the user terminal (default:YELLOW)")
    ap.add_argument("--ground-station-color", default="GREEN", help="Color of the ground station (default:GREEN)")
    ap.add_argument("--with-isl", action='store_true', help="Whether to include ISL visualization (default: False)")

    args = ap.parse_args()

    print(f" Generating Visualization for {args.constellation_name} with {args.minimum_elevation}° elevation...")
    
    generate_extended_visualization(
        constellation_name=args.constellation_name,
        min_elevation_deg=args.minimum_elevation,
        outdir=args.outdir,
        sat_color=args.satellite_color,
        user_color=args.user_color,
        gs_color=args.ground_station_color,
        with_ISL=args.with_isl
    )

if __name__ == "__main__":
    main()