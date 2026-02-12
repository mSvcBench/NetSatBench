def pass_rate(OBJs, oi, data_ext_dict, data_ext_prev_dict, t, dT,
              min_elevation_deg, type, metadata=None):
    """
    Data-rate plugin function.

    Placeholder function to apply link data rate characteristics

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
        Updated row rate_ext[oi, :].

        The input rate_ext row in the current snapshot is initialized
        to zeros and must be filled by this plugin as needed (in Mbit/s).

        Returning None means NetSatBenchGenerate will apply the
        default rate value passed as argument.
    """

    return None
