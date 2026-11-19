#!/usr/bin/env python3
"""Select the llmd gateway located in the same orbit as the access satellite.

    sel = GatewaySelector("llmd-sat-config.yaml")        # Etcd from ETCD_ENDPOINT/ETCD_USER/... env vars
    gw = sel.peek("/tmp/srv6_access.json")                    # -> "sat2" (or None)

- __init__ reads the gateways of the llmd sat config file (written by llmd_emu_sat_deploy.py) and, for each
  gateway node, the orbit from Etcd (/config/nodes/<node> -> metadata.sat_config.sat_orbit). The result is the
  dict `self.gateways`: {gateway sat name: orbit}.
- peek() takes the access satellite as written by srv6/usr_srv6_learn.py (path of its access file, or the loaded
  dict) and returns the name of a gateway in the orbit of that satellite.
"""
import json
import os
import yaml
import sys
from typing import Any, Dict, Optional, Union


def connect_etcd(host: Optional[str] = None, port: Optional[int] = None, user: Optional[str] = None,
                 password: Optional[str] = None, ca_cert: Optional[str] = None):
    """Connect to Etcd. Unset arguments default to ETCD_ENDPOINT (host:port), ETCD_USER, ETCD_PASSWORD, ETCD_CA_CERT."""
    import etcd3
    if host is None:
        endpoint = os.getenv("ETCD_ENDPOINT", "127.0.0.1:2379")
        h, _, p = endpoint.partition(":")
        host, port = h, port or (int(p) if p else 2379)
    port = port or 2379
    user = user or os.getenv("ETCD_USER")
    password = password or os.getenv("ETCD_PASSWORD")
    ca_cert = ca_cert or os.getenv("ETCD_CA_CERT")
    if user and password:
        client = etcd3.client(host=host, port=port, user=user, password=password, ca_cert=ca_cert)
    else:
        client = etcd3.client(host=host, port=port)
    client.status()  # raises if Etcd is not reachable
    return client


class GatewaySelector:
    def __init__(self, config_file: str, etcd_client=None, **etcd_args):
        """config_file: llmd sat config yaml ({"gateways": [{"node": ...}, ...], ...}).
        etcd_client: an etcd3 client; if None one is created with connect_etcd(**etcd_args)."""
        self.etcd = etcd_client if etcd_client is not None else connect_etcd(**etcd_args)
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        self.gateways: Dict[str, int] = {}     # gateway sat name -> orbit (config order is kept)
        for gw in cfg.get("gateways", []):
            name = gw["node"]
            orbit = self._orbit_of(name)
            if orbit is None:
                print(f"warning: orbit of gateway '{name}' not found in Etcd, ignored", file=sys.stderr)
                continue
            self.gateways[name] = orbit

    def _orbit_of(self, sat_name: str) -> Optional[int]:
        value, _ = self.etcd.get(f"/config/nodes/{sat_name}")
        if not value:
            return None
        orbit = json.loads(value.decode()).get("metadata", {}).get("sat_config", {}).get("sat_orbit")
        return int(orbit) if orbit is not None else None

    def peek(self, access_sat: Union[str, Dict[str, Any]], gateways: Optional[Dict[str, int]] = None) -> Optional[str]:
        """Return the name of the gateway in the orbit of the access satellite, or None if there is none.

        access_sat: path of the access file written by usr_srv6_learn.py, or its content as a dict
                    ({"status": "connected", "satellite": "sat299", "orbit": 7, ...}).
        gateways:   dict {gateway name: orbit}; defaults to the one built by __init__.
        If several gateways are in that orbit, the first one of the config file is returned."""
        if isinstance(access_sat, str):
            try:
                with open(access_sat, "r", encoding="utf-8") as f:
                    access_sat = json.load(f)
            except (OSError, ValueError):
                return None
        if access_sat.get("status") != "connected" or access_sat.get("orbit") is None:
            return None
        orbit = int(access_sat["orbit"])
        for name, gw_orbit in (self.gateways if gateways is None else gateways).items():
            if gw_orbit == orbit:
                return name
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Print the llmd gateway in the orbit of the access satellite")
    ap.add_argument("config", help="llmd sat config yaml (e.g. llmd-sat-config.yaml)")
    ap.add_argument("--access-file", default="/tmp/srv6_access.json", help="file written by usr_srv6_learn.py")
    args = ap.parse_args()
    selector = GatewaySelector(args.config)
    print(selector.gateways)
    print(selector.peek(args.access_file))
