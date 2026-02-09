# #!/usr/bin/env python3
import numpy as np
def mbb_antenna(OBJs, oi, data_ext_dict, data_ext_prev_dict, t, dT, min_elevation_deg, type, metadata=None):
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


    # reduce number of links with following priority: keep old links with rising angle, then old links with setting angle, then new links with rising angle and finally new links with setting angle. The number of links kept is equal to the number of antennas -1 (since one link is reserved for control/telemetry and make before break management).
    linked_sats = np.where(delay_data[oi, :] != 0)[0]
    linked_sats_hold = np.array([]) # initialize the array of linked satellites to hold
    linked_sats_prev = np.where(delay_data_prev[oi, :] != 0)[0]
    link_sat_old = np.intersect1d(linked_sats, linked_sats_prev)
    linked_sat_old_rising = link_sat_old[angle_data[oi, link_sat_old] > angle_data_prev[oi, link_sat_old]]
    linked_sat_old_rising = linked_sat_old_rising[np.argsort(angle_data[oi, linked_sat_old_rising])]
    linked_sat_old_setting = link_sat_old[angle_data[oi, link_sat_old] <= angle_data_prev[oi, link_sat_old]]
    linked_sat_old_setting = linked_sat_old_setting[np.argsort(-angle_data[oi, linked_sat_old_setting])]
    linked_sat_new = np.setdiff1d(linked_sats, linked_sats_prev)
    linked_sat_new_rising = linked_sat_new[angle_data[oi, linked_sat_new] > angle_data_prev[oi, linked_sat_new]]
    linked_sat_new_rising = linked_sat_new_rising[np.argsort(angle_data[oi, linked_sat_new_rising])]
    linked_sat_new_setting = linked_sat_new[angle_data[oi, linked_sat_new] <= angle_data_prev[oi, linked_sat_new]]
    linked_sat_new_setting = linked_sat_new_setting[np.argsort(-angle_data[oi, linked_sat_new_setting])]
    linked_sats_sorted = np.concatenate((linked_sat_old_rising, linked_sat_old_setting, linked_sat_new_rising, linked_sat_new_setting))
    linked_sats_hold = linked_sats_sorted[:antenna_count - 1]
    
    # make-before-break
    if len(linked_sats_hold)==1:
        # make before break management
        # estimated angular speed (dT should be small enough to make the estimation accurate)
        angular_speed_exp = (angle_data[oi, linked_sats_hold[-1]] - angle_data_prev[oi, linked_sats_hold[-1]]) / dT
        if angle_data[oi, linked_sats_hold[-1]] + angular_speed_exp * dT < 90 + min_elevation_deg:
            # if the single link is going to be deleted in the next time step due to elevation drop, then add another link for handover to maintain connectivity. The additional link is selected based on the same priority as above.
            if len(linked_sats_sorted) > 1:
                linked_sats_hold = linked_sats_sorted[:antenna_count] 
    
    linked_sat_to_delete = np.setdiff1d(linked_sats, linked_sats_hold)
    delay_data[oi, linked_sat_to_delete] = 0
    return delay_data[oi, :]

    
    

    
    
