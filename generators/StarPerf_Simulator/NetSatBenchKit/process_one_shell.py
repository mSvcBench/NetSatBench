
from typing import Optional


def process_one_shell(shell_name: Optional[str], 
                              GSs, 
                              USERs, 
                              SATs,
                              h5_pos_root, h5_del_root, 
                              h5_pos_root_ext, h5_del_root_ext, h5_type_root_ext, h5_rate_root_ext, h5_loss_root_ext, 
                              gs_ext_conn_function=None, usr_ext_conn_function=None, sat_ext_conn_function=None,
                              rate=None, loss=None, dT=None):
            
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
                raise RuntimeError(f"No timeslot datasets under /position/{shell_name or ''}.")

            # Satellite count from first timeslot
            first_pos = h5_pos_root[timeslots[0]][:]
            n_sat = int(first_pos.shape[0])
            n_tot = n_sat + n_gs + n_usrs

            # store previous timeslot's info for potential sat to ground connection policy (e.g. if you want to only connect GS to certain satellites based on ISL connectivity, you can use this info to avoid redundant elevation calculations)
            del_ext_previous = None
            pos_ext_previous = None
            rate_ext_previous = None
            loss_ext_previous = None

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

                # Add rate and loss for ISLs based on sat_conn_function if provided
                data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext}
                data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous}
                for i in range(0, n_sat):    # skip index 0 as per StarPerf delay h5 convention
                    for j in range(i+1, n_sat):  # only upper triangle for ISL pairs
                        if del_ext[i,j] != 0:  # if there's a link
                            if sat_ext_conn_function.get("rate", None) is not None:
                                new_rate_ext_value = sat_ext_conn_function["rate"](OBJs=SATs, oi=i, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="sat")
                                if new_rate_ext_value is not None:
                                    rate_ext[i,j] = new_rate_ext_value
                                    rate_ext[j,i] = new_rate_ext_value
                                elif rate.get("isl"):
                                    rate_ext[i,j] = rate["isl"]
                                    rate_ext[j,i] = rate["isl"]
                            elif rate.get("isl"):
                                rate_ext[i,j] = rate["isl"]
                                rate_ext[j,i] = rate["isl"]
                            
                            if sat_ext_conn_function.get("loss", None) is not None:
                                new_loss_ext_value = sat_ext_conn_function["loss"](OBJs=SATs, oi=i, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="sat")
                                if new_loss_ext_value is not None:
                                    loss_ext[i,j] = new_loss_ext_value
                                    loss_ext[j,i] = new_loss_ext_value
                                elif loss.get("isl"):
                                    loss_ext[i,j] = loss["isl"]
                                    loss_ext[j,i] = loss["isl"]
                            elif loss.get("isl"):
                                loss_ext[i,j] = loss["isl"]
                                loss_ext[j,i] = loss["isl"]
                
                for gi, gsp_ecef in enumerate(gs_pos_ecef):
                    gidx = n_sat + gi
                    for si, satp in enumerate(sat_pos):
                        sidx = si
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge, _ = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           gsp_ecef[0] , gsp_ecef[1] , gsp_ecef[2] ,
                                                           min_elevation_deg)
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - gsp_ecef[0]) ** 2
                                + (spos_ecef[1] - gsp_ecef[1]) ** 2
                                + (spos_ecef[2] - gsp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            del_ext[sidx, gidx] = delay_s
                            del_ext[gidx, sidx] = delay_s
                    
                    # remove sat-gs links due to antennas/processing limitations and append rate and loss info based on gs-defined plugin if provided
                    if del_ext[gidx, sidx]!=0:
                        # link theoretically exist based on elevation, but check if user wants to remove it due to antenna limitations or other policies by passing info to plugin
                        data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext}
                        data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous}
                        
                        if gs_ext_conn_function.get("antenna", None) is not None:
                            new_del_ext_value = gs_ext_conn_function["antenna"](OBJs=GSs, oi=gidx, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="gs")
                            if new_del_ext_value is not None:
                                del_ext[sidx, gidx] = new_del_ext_value
                                del_ext[gidx, sidx] = new_del_ext_value
                        
                        if gs_ext_conn_function.get("rate", None) is not None:   
                            new_rate_ext_value = gs_ext_conn_function["rate"](OBJs=GSs, oi=gidx, del_ext=del_ext, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="gs")
                            if new_rate_ext_value is not None:
                                rate_ext[sidx, gidx] = new_rate_ext_value
                                rate_ext[gidx, sidx] = new_rate_ext_value
                            elif rate.get("gs"):
                                rate_ext[sidx, gidx] = rate["gs"]
                                rate_ext[gidx, sidx] = rate["gs"]
                        elif rate.get("gs"):
                            rate_ext[sidx, gidx] = rate["gs"]
                            rate_ext[gidx, sidx] = rate["gs"]
                        
                        if gs_ext_conn_function.get("loss", None) is not None:    
                            new_loss_ext_value = gs_ext_conn_function["loss"](OBJs=GSs, oi=gidx, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="gs")
                            if new_loss_ext_value is not None:
                                loss_ext[sidx, gidx] = new_loss_ext_value
                                loss_ext[gidx, sidx] = new_loss_ext_value
                            elif loss.get("gs"):
                                loss_ext[sidx, gidx] = loss["gs"]
                                loss_ext[gidx, sidx] = loss["gs"]
                        elif loss.get("gs"):
                            loss_ext[sidx, gidx] = loss["gs"]
                            loss_ext[gidx, sidx] = loss["gs"]
                
                for ui, usp_ecef in enumerate(usr_pos_ecef):
                    uidx = n_sat + n_gs + 1 + ui
                    for si, satp in enumerate(sat_pos):
                        sidx = si + 1
                        transient_object = SAT.satellite(0,0,False)
                        transient_object.longitude = float(satp[0])
                        transient_object.latitude = float(satp[1])
                        transient_object.altitude = float(satp[2])
                        spos_ecef = latilong_to_descartes(transient_object)  # get GS position in ECEF
                        elev_judge, _ = judgePointToSatellite(spos_ecef[0] , spos_ecef[1] , spos_ecef[2] ,
                                                           usp_ecef[0] , usp_ecef[1] , usp_ecef[2] ,
                                                           min_elevation_deg)
                        if elev_judge:
                            distance = math.sqrt(
                                (spos_ecef[0] - usp_ecef[0]) ** 2
                                + (spos_ecef[1] - usp_ecef[1]) ** 2
                                + (spos_ecef[2] - usp_ecef[2]) ** 2
                            ) / 1000  # in km
                            delay_s = distance / 300000
                            del_ext[sidx, uidx] = delay_s
                            del_ext[uidx, sidx] = delay_s
                    
                    # remove sat-user links due to antennas/processing limitations and append rate and loss info based on user-defined plugin if provided
                    if del_ext[sidx, uidx]!=0:
                        # link theoretically exist based on elevation, but check if user wants to remove it due to antenna limitations or other policies by passing info to plugin
                        data_ext_dict = {"delay": del_ext, "rate": rate_ext, "loss": loss_ext, "pos": pos_ext}
                        data_ext_prev_dict = {"delay": del_ext_previous, "rate": rate_ext_previous, "loss": loss_ext_previous, "pos": pos_ext_previous}
                        
                        if usr_ext_conn_function.get("antenna", None) is not None:
                            new_del_ext_value = usr_ext_conn_function["antenna"](OBJs=USERs, oi=ui, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="user")
                            if new_del_ext_value is not None:
                                del_ext[sidx, uidx] = new_del_ext_value
                                del_ext[uidx, sidx] = new_del_ext_value
                        
                        if usr_ext_conn_function.get("rate", None) is not None:   
                            new_rate_ext_value = usr_ext_conn_function["rate"](OBJs=USERs, oi=ui, del_ext=del_ext, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="user")
                            if new_rate_ext_value is not None:
                                rate_ext[sidx, uidx] = new_rate_ext_value
                                rate_ext[uidx, sidx] = new_rate_ext_value
                            elif rate.get("user"):
                                rate_ext[sidx, uidx] = rate["user"]
                                rate_ext[uidx, sidx] = rate["user"]
                        elif rate.get("user"):
                            rate_ext[sidx, uidx] = rate["user"]
                            rate_ext[uidx, sidx] = rate["user"]
                        
                        if usr_ext_conn_function.get("loss", None) is not None:    
                            new_loss_ext_value = usr_ext_conn_function["loss"](OBJs=USERs, oi=ui, data_ext_dict=data_ext_dict, data_ext_prev_dict=data_ext_prev_dict, dT=dT, type="user")
                            if new_loss_ext_value is not None:
                                loss_ext[sidx, uidx] = new_loss_ext_value
                                loss_ext[uidx, sidx] = new_loss_ext_value
                            elif loss.get("user"):
                                loss_ext[sidx, uidx] = loss["user"]
                                loss_ext[uidx, sidx] = loss["user"]
                        elif loss.get("user"):
                            loss_ext[sidx, uidx] = loss["user"]
                            loss_ext[uidx, sidx] = loss["user"]
                
                # store current timeslot's info for potential use in next timeslot's connection policy
                del_ext_previous = del_ext
                pos_ext_previous = pos_ext
                rate_ext_previous = rate_ext
                loss_ext_previous = loss_ext

                # Write datasets
                if ts in h5_pos_root_ext:
                    if overwrite:
                        del h5_pos_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"Dataset {h5_pos_root_ext.name}/{ts} already exists (use --overwrite-gs-groups)."
                        )
                if ts in h5_del_root_ext:
                    if overwrite:
                        del h5_del_root_ext[ts]
                    else:
                        raise RuntimeError(
                            f"Dataset {h5_del_root_ext.name}/{ts} already exists (use --overwrite-gs-groups)."
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