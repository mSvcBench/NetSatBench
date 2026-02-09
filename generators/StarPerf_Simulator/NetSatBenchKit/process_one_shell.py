
import argparse
from pathlib import Path
import sys
import h5py
import numpy as np
import math
import sys
# import from upper dir for constellation generation and connectivity plugins
sys.path.append(str(Path(__file__).parent.parent))  # adjust as needed
import src.XML_constellation.constellation_entity.satellite as SAT
from typing import Optional

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

def process_one_shell(shell_name: Optional[str], 
                              GSs, 
                              USERs, 
                              SATs,
                              h5_pos_root, h5_del_root, 
                              h5_pos_root_ext, h5_del_root_ext, h5_type_root_ext, h5_rate_root_ext, h5_loss_root_ext, 
                              gs_ext_conn_function=None, usr_ext_conn_function=None, sat_ext_conn_function=None,
                              min_elevation_deg=25, rate=None, loss=None, dT=15, overwrite=True):
            
            # precompute Users and GSs ECEF 
            n_gs = len(GSs)
            n_usrs = len(USERs)
            gs_pos = np.array([(0.0, 0.0, 0.0)] * n_gs)  # placeholder for GS positions in (longitude, latitude, altitude)
            gs_pos_ecef = np.array([(0.0, 0.0, 0.0)] * n_gs)  # placeholder for GS positions in ECEF
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
        
            # Sort timeslots
            timeslots = sorted(
                h5_pos_root.keys(),
                key=lambda s: int("".join(ch for ch in s if ch.isdigit()) or "0"),
            )
            if not timeslots:
                raise RuntimeError(f"❌ No timeslot datasets under /position/{shell_name or ''}.")

            # Satellite count from first timeslot
            first_pos = h5_pos_root[timeslots[0]][:]
            n_sat = int(first_pos.shape[0])
            n_tot = n_sat + n_gs + n_usrs

            # store previous timeslot's info for potential sat to ground connection policy (e.g. if you want to only connect GS to certain satellites based on ISL connectivity, you can use this info to avoid redundant elevation calculations)
            del_ext_previous = None
            pos_ext_previous = None
            rate_ext_previous = None
            loss_ext_previous = None
            angle_ext_previous = None

            NODEs = SATs + GSs + USERs  # combined list of all objects for plugin processing, ordered by satellite first then GS then USER as per extended matrix construction

            for ts in timeslots:
                sat_pos = h5_pos_root[ts][:]        # (n_sat, 3) longitude, latitude, altitude
                isl_del = h5_del_root[ts][:,:]  # (n_sat+1, n_sat+1) #  first row/col left void by StarPerf
                # fix possible first void rw/col in isl_delay by StarPerf convention (if not already all zeros)
                if isl_del.shape[0] == n_sat + 1 and isl_del.shape[1] == n_sat + 1:
                    # remove fist row and column to get pure satellite-satellite delay matrix for plugin processing
                    isl_del = isl_del[1:, 1:]

                pos_ext = None
                if n_gs > 0:
                    pos_ext = np.vstack([sat_pos.astype("float64", copy=False), gs_pos])
                else:
                    pos_ext = sat_pos.astype("float64", copy=False)
                
                if n_usrs > 0:
                    pos_ext =  np.vstack([pos_ext, usr_pos_ecef])

                # Extended delay: copy sat-sat then fill sat-gs
                del_ext = np.zeros((n_tot, n_tot), dtype="float64") # initialize with zeros (no links) and first row/col void as per StarPerf convention
                del_ext[:n_sat, :n_sat] = isl_del
                
                # Extended rate and loss
                rate_ext = np.zeros((n_tot, n_tot), dtype="float64")  # store bit rate in Mbps and first row/col void as per StarPerf convention
                loss_ext = np.zeros((n_tot, n_tot), dtype="float64")  # store loss rate as float between 0 and 1 and first row/col void as per StarPerf convention

                # Supporting anngles for potential use in connectivity plugins without redundant calculations
                angle_ext = np.zeros((n_tot, n_tot), dtype="float64")  # store elevation angle in degrees and first row/col void as per StarPerf convention
                # Add rate and loss for ISLs based on sat_conn_function if provided
                
                data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext, "angle": angle_ext}
                data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous, "angle": angle_ext_previous}
                for i in range(0, n_sat):    # skip index 0 as per StarPerf delay h5 convention
                    linked_sats = np.where(del_ext[i,:] != 0)[0]  # check which satellites are linked to satellite i based on delay matrix (non-zero entries)
                    if sat_ext_conn_function.get("rate", None) is not None:
                        new_rate_ext_values = sat_ext_conn_function["rate"](
                            OBJs=NODEs, oi=i, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, 
                            type="sat", metadata=sat_ext_conn_function.get("rate_metadata", None))
                        if new_rate_ext_values is not None:
                            rate_ext[i,:] = new_rate_ext_values.copy()  
                            rate_ext[:,i] = new_rate_ext_values.copy() 
                        elif rate.get("isl"):
                            rate_ext[i,linked_sats] = rate["isl"]
                            rate_ext[linked_sats,i] = rate["isl"]
                    elif rate.get("isl"):
                        rate_ext[i,linked_sats] = rate["isl"]
                        rate_ext[linked_sats,i] = rate["isl"]
                    
                    if sat_ext_conn_function.get("loss", None) is not None:
                        new_loss_ext_value = sat_ext_conn_function["loss"](
                            OBJs=NODEs, oi=i, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, 
                            type="sat", metadata=sat_ext_conn_function.get("loss_metadata", None))
                        if new_loss_ext_value is not None:
                            loss_ext[i,:] = new_loss_ext_value.copy()
                            loss_ext[:,i] = new_loss_ext_value.copy()
                        elif loss.get("isl"):
                            loss_ext[i,linked_sats] = loss["isl"]
                            loss_ext[linked_sats,i] = loss["isl"]
                    elif loss.get("isl"):
                        loss_ext[i,linked_sats] = loss["isl"]
                        loss_ext[linked_sats,i] = loss["isl"]
                
                for gi, gsp_ecef in enumerate(gs_pos_ecef):
                    gidx = n_sat + gi
                    for si, satp in enumerate(sat_pos):
                        sidx = si
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge, angle = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           gsp_ecef[0] , gsp_ecef[1] , gsp_ecef[2] ,
                                                           min_elevation_deg)
                        angle_ext[sidx, gidx] = angle
                        angle_ext[gidx, sidx] = angle
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - gsp_ecef[0]) ** 2
                                + (spos_ecef[1] - gsp_ecef[1]) ** 2
                                + (spos_ecef[2] - gsp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            del_ext[sidx, gidx] = delay_s
                            del_ext[gidx, sidx] = delay_s

                    
                    # link theoretically exist based on elevation, 
                    # but check if user wants to remove it due to antenna limitations or other policies by passing info to plugin
                    data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext, "angle": angle_ext}
                    data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous, "angle": angle_ext_previous}
                    
                    if gs_ext_conn_function.get("antenna", None) is not None:
                        new_del_ext_values = gs_ext_conn_function["antenna"](
                            OBJs=NODEs, oi=gidx, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, 
                            type="gs", metadata=gs_ext_conn_function.get("antenna_metadata", None))
                        if new_del_ext_values is not None:
                            del_ext[:, gidx] = new_del_ext_values.copy()  
                            del_ext[gidx, :] = new_del_ext_values.copy()
                    
                    # compute rate and loss for remaining links after antenna plugin processing (if any) based on user-defined functions or static values
                    linked_sats = np.where(del_ext[:, gidx] != 0)[0]
                    if gs_ext_conn_function.get("rate", None) is not None:   
                        new_rate_ext_values = gs_ext_conn_function["rate"](
                            OBJs=NODEs, oi=gidx, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, type="gs", 
                            metadata=gs_ext_conn_function.get("rate_metadata", None))
                        if new_rate_ext_values is not None:
                            rate_ext[:, gidx] = new_rate_ext_values.copy()  
                            rate_ext[gidx, :] = new_rate_ext_values.copy()
                        elif rate.get("gs"):
                            rate_ext[linked_sats, gidx] = rate["gs"]
                            rate_ext[gidx, linked_sats] = rate["gs"]
                    elif rate.get("gs"):
                        rate_ext[linked_sats, gidx] = rate["gs"]
                        rate_ext[gidx, linked_sats] = rate["gs"]
                    
                    if gs_ext_conn_function.get("loss", None) is not None:    
                        new_loss_ext_value = gs_ext_conn_function["loss"](
                            OBJs=NODEs, oi=gidx, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, 
                            type="gs", metadata=gs_ext_conn_function.get("loss_metadata", None))
                        if new_loss_ext_value is not None:
                            loss_ext[:, gidx] = new_loss_ext_value.copy() 
                            loss_ext[gidx, :] = new_loss_ext_value.copy()
                        elif loss.get("gs"):
                            loss_ext[linked_sats, gidx] = loss["gs"]
                            loss_ext[gidx, linked_sats] = loss["gs"]
                    elif loss.get("gs"):
                        loss_ext[linked_sats, gidx] = loss["gs"]
                        loss_ext[gidx, linked_sats] = loss["gs"]
                
                for ui, usp_ecef in enumerate(usr_pos_ecef):
                    uidx = n_sat + n_gs + ui
                    for si, satp in enumerate(sat_pos):
                        sidx = si
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge, angle = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           usp_ecef[0] , usp_ecef[1] , usp_ecef[2] ,
                                                           min_elevation_deg)
                        angle_ext[sidx, uidx] = angle
                        angle_ext[uidx, sidx] = angle
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - usp_ecef[0]) ** 2
                                + (spos_ecef[1] - usp_ecef[1]) ** 2
                                + (spos_ecef[2] - usp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            del_ext[sidx, uidx] = delay_s
                            del_ext[uidx, sidx] = delay_s
                               
                    # link theoretically exist based on elevation, but check if user wants to remove it due to antenna limitations or other policies by passing info to plugin
                    data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext, "angle": angle_ext}
                    data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous, "angle": angle_ext_previous}

                    if usr_ext_conn_function.get("antenna", None) is not None:
                        new_del_ext_values = usr_ext_conn_function["antenna"](
                            OBJs=NODEs, oi=uidx, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg, 
                            type="user", metadata=usr_ext_conn_function.get("antenna_metadata", None))
                        if new_del_ext_values is not None:
                            del_ext[:, uidx] = new_del_ext_values.copy()
                            del_ext[uidx, :] = new_del_ext_values.copy()
                    
                    # compute rate and loss for remaining links after antenna plugin processing (if any) based on user-defined functions or static values
                    linked_sats = np.where(del_ext[:, uidx] != 0)[0]
                    if usr_ext_conn_function.get("rate", None) is not None:   
                        new_rate_ext_values = usr_ext_conn_function["rate"](
                            OBJs=NODEs, oi=uidx, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg,
                            type="user", metadata=usr_ext_conn_function.get("rate_metadata", None))
                        if new_rate_ext_values is not None:
                            rate_ext[:, uidx] = new_rate_ext_values.copy()
                            rate_ext[uidx, :] = new_rate_ext_values.copy()
                        elif rate.get("user"):
                            rate_ext[linked_sats, uidx] = rate["user"]
                            rate_ext[uidx, linked_sats] = rate["user"]
                    elif rate.get("user"):
                        rate_ext[linked_sats, uidx] = rate["user"]
                        rate_ext[uidx, linked_sats] = rate["user"]
                    
                    if usr_ext_conn_function.get("loss", None) is not None:    
                        new_loss_ext_value = usr_ext_conn_function["loss"](
                            OBJs=NODEs, oi=uidx, 
                            data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, 
                            t=ts, dT=dT, min_elevation_deg=min_elevation_deg,
                            type="user", metadata=usr_ext_conn_function.get("loss_metadata", None))
                        if new_loss_ext_value is not None:
                            loss_ext[:, uidx] = new_loss_ext_value.copy()
                            loss_ext[uidx, :] = new_loss_ext_value.copy()
                        elif loss.get("user"):
                            loss_ext[linked_sats, uidx] = loss["user"]
                            loss_ext[uidx, linked_sats] = loss["user"]
                    elif loss.get("user"):
                        loss_ext[linked_sats, uidx] = loss["user"]
                        loss_ext[uidx, linked_sats] = loss["user"]
                
                # store current timeslot's info for potential use in next timeslot's connection policy
                del_ext_previous = del_ext
                pos_ext_previous = pos_ext
                rate_ext_previous = rate_ext
                loss_ext_previous = loss_ext
                angle_ext_previous = angle_ext

                # Write datasets
                if ts in h5_pos_root_ext:
                    if overwrite:
                        del h5_pos_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"❌ Dataset {h5_pos_root_ext.name}/{ts} already exists (use --overwrite-ext-groups)."
                        )
                if ts in h5_del_root_ext:
                    if overwrite:
                        del h5_del_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"❌ Dataset {h5_del_root_ext.name}/{ts} already exists (use --overwrite-ext-groups)."
                        )
                if ts in h5_rate_root_ext:
                    if overwrite:
                        del h5_rate_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"❌ Dataset {h5_rate_root_ext.name}/{ts} already exists (use --overwrite-ext-groups)."
                        )
                if ts in h5_loss_root_ext:
                    if overwrite:
                        del h5_loss_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"❌ Dataset {h5_loss_root_ext.name}/{ts} already exists (use --overwrite-ext-groups)."
                        )
                
                h5_del_root_ext.create_dataset(ts, data=del_ext, compression="gzip", compression_opts=4)
                h5_pos_root_ext.create_dataset(ts, data=pos_ext, compression="gzip", compression_opts=4)              
                h5_rate_root_ext.create_dataset(ts, data=rate_ext, compression="gzip", compression_opts=4)
                h5_loss_root_ext.create_dataset(ts, data=loss_ext, compression="gzip", compression_opts=4)
                
            # Write type_ext dataset
            type_ext = np.array([b"sat"] * n_tot, dtype='S')
            for gi in range(n_gs):
                type_ext[n_sat + gi] = b"gs"
            for ui in range(n_usrs):
                type_ext[n_sat + n_gs + ui] = b"user"
            h5_type_root_ext.create_dataset("type_ext", data=type_ext, compression="gzip", compression_opts=4)