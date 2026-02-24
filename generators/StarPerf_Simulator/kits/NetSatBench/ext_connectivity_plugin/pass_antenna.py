# #!/usr/bin/env python3
def pass_antenna(OBJs, oi, data_ext_dict, data_ext_prev_dict, t, dT,
                 min_elevation_deg, type, metadata=None):
    """
    Antenna-constraint plugin function.

    Placeholder function to apply antenna limitations

    Parameters
    ----------
    OBJs : list
        List of constellation N objects:
            - Satellite objects (src/XML_constellation/constellation_entity/satellite.py)
            - Ground station objects (src/XML_constellation/constellation_entity/ground_station.py)
            - User objects (NetSatBencKit/NetSatBenchGenerate.py)

    oi : int
        Index of the object for which the plugin is invoked.

    data_ext_dict : dict
        Extended data of the current snapshot containing:
            - "del_ext"  : (N, N) numpy link delay matrix [s]
            - "rate_ext" : (N, N) numpy link data rate matrix [bps]
            - "loss_ext" : (N, N) numpy packet loss matrix [0–1]
            - "pos_ext"  : (N, 3) numpy object positions (lon, lat, alt)
            - "angle_ext": (N, N) numpy elevation angles [deg]
              (valid for sat–ground and sat–user links)

    data_ext_prev_dict : dict
        Extended data of the previous snapshot (same structure).

    t : float
        Current simulation time [s].

    dT : float
        Time step between simulation snapshots [s].

    min_elevation_deg : float
        Minimum elevation angle [deg] required to establish a link.

    type : str
        Object type: "sat", "gs", or "user".

    metadata : str, optional
        File path for accessing additional user-defined information.

    Returns
    -------
    numpy.ndarray or None
        Updated row del_ext[oi, :].

        The input del_ext row contains all geometrically valid links
        (non-zero delay means link is theoretically feasible).

        The function must modify this row according to antenna
        constraints (e.g., maximum number of active links,
        make-before-break handling, etc.). 
        For instance, setting del_ext[oi, j] = 0 can be used to drop the link between object oi and object j.

        Returning None means no modification is applied and the
        original link set remains unchanged.
    """
    # This is equivalent to return None
    # del_ext = data_ext_dict.get("del_ext", None)
    # del_ext_updated_raw = del_ext[oi,:].copy 
    
    return None
