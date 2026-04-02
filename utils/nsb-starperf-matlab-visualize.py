#!/usr/bin/env python3

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def matlab_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def matlab_bool(value: bool) -> str:
    return "true" if value else "false"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch NetSatBenchMatlabVisualizer.m in MATLAB desktop from the StarPerf_Simulator directory."
    )
    parser.add_argument("--matlab-path",
                        help="Optional path to the MATLAB executable. Defaults to searching PATH.")
    parser.add_argument("--constellation-name", required=True,
                        help="Constellation name used to resolve XML inputs under config/.")
    parser.add_argument("--h5", required=True,
                        help="Path to the generated HDF5 file, relative to generators/StarPerf_Simulator/ or absolute.")
    parser.add_argument("--user-xml",
                        help="Optional override for the users XML path.")
    parser.add_argument("--gateway-xml",
                        help="Optional override for the ground stations XML path.")
    parser.add_argument("--constellation-xml",
                        help="Optional override for the constellation XML path.")
    parser.add_argument("--selected-shell", type=int, default=1)
    parser.add_argument("--add-user-access", action="store_true")
    parser.add_argument("--add-gateway-access", action="store_true")
    parser.add_argument("--add-isl", action="store_true")
    parser.add_argument("--inter-plane-offset", type=int, default=0)
    parser.add_argument("--user-min-elevation-angle", type=float, default=25.0)
    parser.add_argument("--gateway-min-elevation-angle", type=float, default=25.0)
    parser.add_argument("--start-time",
                        help="MATLAB datetime expression, e.g. datetime(2023,10,1,0,0,0)")
    parser.add_argument("--stop-time",
                        help="MATLAB datetime expression, e.g. datetime(2023,10,1,0,15,0)")
    parser.add_argument("--sample-time", type=float, default=30.0)
    parser.add_argument("--cache-file",
                        help="Optional MAT cache file path.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable reuse of cached visualization data.")
    parser.add_argument("--show-details", action="store_true")

    args = parser.parse_args()

    matlab = args.matlab_path or shutil.which("matlab")
    if matlab is None:
        print("MATLAB executable not found on PATH. Install MATLAB or add it to PATH before using this command.")
        return 1

    root = Path.cwd()
    constellation_xml = args.constellation_xml or f"config/XML_constellation/{args.constellation_name}.xml"
    user_xml = args.user_xml or f"config/users/{args.constellation_name}.xml"
    gateway_xml = args.gateway_xml or f"config/ground_stations/{args.constellation_name}.xml"

    h5_path = Path(args.h5)
    if not h5_path.is_absolute():
        h5_path = root / h5_path
    h5_path = h5_path.resolve()

    call_parts = [
        "addpath('kits/NetSatBench')",
        "NetSatBenchMatlabVisualizer("
        + ", ".join([
            matlab_string(constellation_xml),
            matlab_string(str(h5_path)),
            matlab_string(user_xml),
            matlab_string(gateway_xml),
            matlab_string("SelectedShell"),
            str(args.selected_shell),
            matlab_string("AddUserAccess"),
            matlab_bool(args.add_user_access),
            matlab_string("AddGatewayAccess"),
            matlab_bool(args.add_gateway_access),
            matlab_string("AddISL"),
            matlab_bool(args.add_isl),
            matlab_string("InterPlaneOffset"),
            str(args.inter_plane_offset),
            matlab_string("UserMinElevationAngle"),
            str(args.user_min_elevation_angle),
            matlab_string("GatewayMinElevationAngle"),
            str(args.gateway_min_elevation_angle),
            matlab_string("SampleTime"),
            str(args.sample_time),
            matlab_string("UseCache"),
            matlab_bool(not args.no_cache),
            matlab_string("ShowDetails"),
            matlab_bool(args.show_details),
        ])
        + ")",
    ]

    if args.cache_file:
        call_parts[-1] = call_parts[-1][:-1] + ", " + ", ".join([
            matlab_string("CacheFile"),
            matlab_string(args.cache_file),
        ]) + ")"
    if args.start_time:
        call_parts[-1] = call_parts[-1][:-1] + ", " + ", ".join([
            matlab_string("StartTime"),
            args.start_time,
        ]) + ")"
    if args.stop_time:
        call_parts[-1] = call_parts[-1][:-1] + ", " + ", ".join([
            matlab_string("StopTime"),
            args.stop_time,
        ]) + ")"

    matlab_command = "; ".join(call_parts)
    result = subprocess.run([matlab, "-r", matlab_command], cwd=root)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
