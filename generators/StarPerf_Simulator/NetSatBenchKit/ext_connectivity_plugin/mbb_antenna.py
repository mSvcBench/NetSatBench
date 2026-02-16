# #!/usr/bin/env python3
import numpy as np
import retain_antenna as ra

def mbb_antenna(OBJs, oi, data_ext_dict, data_ext_prev_dict, t, dT,
                min_elevation_deg, type, metadata=None):
    """
    Make-before-break (MBB) antenna management plugin.

    Enforces antenna constraints for user and ground station objects and
    updates the connectivity (del_ext[oi, :]) accordingly.

    Policy
    ------
    - Each object has `antenna_count` antennas.
    - Keep at most `antenna_count - 1` active links in steady state.
      One antenna is reserved for temporary make-before-break handover.

    Link selection (when more candidates than allowed)
    --------------------------------------------------
    1) Prefer already active links (minimize unnecessary handovers).
    2) Prefer links with increasing elevation angle (angle rising).
    3) Prefer links with higher elevation angle.

    Make-before-break behavior
    --------------------------
    - If only one link can be held and that link is predicted to be dropped
      in the next time step due to elevation decrease, proactively add a
      second link for handover before releasing the first one.
    - The additional link is selected using the same priority rules above.

    Returns
    -------
    numpy.ndarray or None
        Updated row del_ext[oi, :]. Returning None means no change.
    """


    if type in ["gs", "user"]:
        longitude = OBJs[oi].longitude # the longitude of USER
        latitude = OBJs[oi].latitude # the latitude of USER
        frequency = OBJs[oi].frequency # the frequency of User, such as Ka,E and so on
        antenna_count = OBJs[oi].antenna_count # the number of antenna of USER
        uplink_GHz = OBJs[oi].uplink_GHz # the uplink GHz of USER
        downlink_GHz = OBJs[oi].downlink_GHz # the downlink GHz of USER
    else :
        # plug in has no impact on satellite objects
        return None
    
    delay_data = data_ext_dict.get("delay", None).copy()
    angle_data = data_ext_dict.get("angle", None).copy()
    delay_data_prev = data_ext_prev_dict.get("delay")
    angle_data_prev = data_ext_prev_dict.get("angle")
    if delay_data_prev is None:
        delay_data_prev = delay_data.copy()
    if angle_data_prev is None:
        angle_data_prev = angle_data.copy()

    # retain policy with antenna_count-1
    linked_sats = np.where(delay_data[oi, :] != 0)[0]
    linked_sats_updated = np.array([]) # initialize the array of updated linked satellites
    linked_sats_prev = np.where(delay_data_prev[oi, :] != 0)[0]
    link_sat_old = np.intersect1d(linked_sats, linked_sats_prev) # common links in previous and current snapshot
    linked_sat_old_rising = link_sat_old[angle_data[oi, link_sat_old] > angle_data_prev[oi, link_sat_old]]
    linked_sat_old_rising = linked_sat_old_rising[np.argsort(angle_data[oi, linked_sat_old_rising])]
    linked_sat_old_setting = link_sat_old[angle_data[oi, link_sat_old] <= angle_data_prev[oi, link_sat_old]]
    linked_sat_old_setting = linked_sat_old_setting[np.argsort(-angle_data[oi, linked_sat_old_setting])]
    linked_sat_new = np.setdiff1d(linked_sats, linked_sats_prev) # new links in the current snapshot
    linked_sat_new_rising = linked_sat_new[angle_data[oi, linked_sat_new] > angle_data_prev[oi, linked_sat_new]]
    linked_sat_new_rising = linked_sat_new_rising[np.argsort(angle_data[oi, linked_sat_new_rising])]
    linked_sat_new_setting = linked_sat_new[angle_data[oi, linked_sat_new] <= angle_data_prev[oi, linked_sat_new]]
    linked_sat_new_setting = linked_sat_new_setting[np.argsort(-angle_data[oi, linked_sat_new_setting])]
    linked_sats_sorted = np.concatenate((linked_sat_old_rising, linked_sat_old_setting, linked_sat_new_rising, linked_sat_new_setting))
    linked_sats_updated = linked_sats_sorted[:antenna_count-1] 

    # make-before-break
    if len(linked_sats_updated)==1:
        # make before break management
        # estimated angular speed (dT should be small enough to make the estimation accurate)
        angular_speed_exp = (angle_data[oi, linked_sats_updated[-1]] - angle_data_prev[oi, linked_sats_updated[-1]]) / dT
        if angle_data[oi, linked_sats_updated[-1]] + angular_speed_exp * dT < 90 + min_elevation_deg:
            # if the single link is going to be deleted in the next time step due to elevation drop, then add another link for handover to maintain connectivity. The additional link is selected based on the same priority as above.
            if len(linked_sats_sorted) > 1:
                # find first candidate for handover that is not going to be dropped in the next time step
                for candidate in linked_sats_sorted[antenna_count-1:]:
                    angular_speed_candidate_exp = (angle_data[oi, candidate] - angle_data_prev[oi, candidate]) / dT
                    if angle_data[oi, candidate] + angular_speed_candidate_exp * dT >= 90 + min_elevation_deg:
                        linked_sats_updated = np.append(linked_sats_updated, candidate)
                        break
    
    linked_sat_to_delete = np.setdiff1d(linked_sats, linked_sats_updated)
    delay_data[oi, linked_sat_to_delete] = 0
    return delay_data[oi, :]

    
    

    
    
