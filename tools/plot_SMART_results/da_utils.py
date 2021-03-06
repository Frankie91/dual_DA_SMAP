 
import numpy as np
import pandas as pd
import os
import string
from collections import OrderedDict
import xarray as xr
import datetime as dt
import multiprocessing as mp
import shutil
import scipy.linalg as la
import glob
import properscoring as ps
from scipy import stats
import pickle

from tonic.models.vic.vic import VIC, default_vic_valgrind_error_code

import timeit


def calculate_sm_noise_to_add_magnitude(vic_history_path, sigma_percent):
    ''' Calculates the standard deviation of Gaussian noise to add for each
        layer and grid cell.

    Parameters
    ----------
    vic_history_path: <str>
        VIC history output file path, typically of openloop run. The range
        of soil moisture for each layer and each grid cell will be used
        to determine soil parameter perturbation level
    sigma_percent: <list>
        List of percentage of the soil moisture range value to perturb.
        Each value of sigma_percent will be used as the standard deviation of
        the Gaussian noise added (e.g., sigma_percent = 5 for 5% of soil 
        moisture range for perturbation).
        Each value in the list is for one soil layer (from up to bottom)

    Returns
    ----------
    da_scale: <xr.DataArray>
        Standard deviation of noise to add
        Dimension: [nlayer, lat, lon]
    '''

    # Load VIC history file and extract soil moisture var
    ds_hist = xr.open_dataset(vic_history_path)
    da_sm = ds_hist['OUT_SOIL_MOIST']
    nlayer = da_sm['nlayer'].values  # coordinate of soil layers

    # Calculate range of soil moisture
    da_range = da_sm.max(dim='time') - da_sm.min(dim='time')

    # Calculate standard devation of noise to add
    da_scale = da_range.copy(deep=True)
    da_scale[:] = np.nan  # [nlayer, lat, lon]
    for i, layer in enumerate(nlayer):
        da_scale.loc[layer, :, :] = da_range.loc[layer, :, :].values\
                                    * sigma_percent[i] / 100.0

    return da_scale


def calculate_cholesky_L(n, corrcoef):
    ''' Calculates covariance matrix for sm state perturbation for one grid cell.

    Parameters
    ----------
    n: <int>
        Total number of EnKF states
    corrcoef: <float>
        Correlation coefficient across different tiles and soil layers

    Returns
    ----------
    L: <np.array>
        Cholesky decomposed matrix of covariance matrix P of all states:
                        P = L * L.T
        Thus, L * Z (where Z is i.i.d. standard normal random variables of
        dimension [n, n]) is multivariate normal random variables with
        mean zero and covariance matrix P.
        Dimension: [n, n]
    '''

    # Calculate P as an correlation coefficient matrix
    P = np.identity(n)
    P[P==0] = corrcoef  # [n, n]

    # Calculate L (Cholesky decomposition: P = L * L.T)
    L = la.cholesky(P).T  # [n, n]

    return L


def calculate_scale_n(layer_scale, nveg, nsnow):
    ''' Calculates covariance matrix for sm state perturbation for one grid cell.

    Parameters
    ----------
    layer_scale: <np.array>
        An array of standard deviation of noise to add for each layer
        Dimension: [nlayer]
    nveg: <int>
        Number of veg classes
    nsnow: <int>
        Number of snow bands

    Returns
    ----------
    scale_n: <np.array>
        Standard deviation of noise to add for a certain grid cell.
        Dimension: [n]
    '''

    nlayer = len(layer_scale)
    n = nlayer * nveg * nsnow

    # Calculate scale_n
    scale_n = np.repeat(layer_scale, nveg*nsnow)  # [n]

    return scale_n


def calculate_scale_n_whole_field(da_scale, nveg, nsnow):
    ''' Calculates covariance matrix for sm state perturbation for one grid cell.

    Parameters
    ----------
    da_scale: <xr.DataArray>
        Standard deviation of noise to add
        Dimension: [nlayer, lat, lon]
    nveg: <int>
        Number of veg classes
    nsnow: <int>
        Number of snow bands

    Returns
    ----------
    scale_n_nloop: <np.array>
        Standard deviation of noise to add for the whole field.
        Dimension: [nloop, n] (where nloop = lat * lon)
        
    Require
    ----------
    calculate_sm_noise_to_add_covariance_matrix
    '''

    # Extract coordinates
    lat = da_scale['lat']
    lon = da_scale['lon']
    nlayer = da_scale['nlayer']
    
    # Determine total number of loops
    nloop = len(lat) * len(lon)
    n = len(nlayer) * nveg * nsnow
    
    # Convert da_scale to np.array and straighten lat and lon into nloop
    scale = da_scale.values.reshape([len(nlayer), nloop])  # [nlayer, nloop]
    scale = np.transpose(scale)  # [nloop, nlayer]
    scale[np.isnan(scale)] = 0  # set inactive cell to zero
    
    # Calculate scale_n for the whole field
    scale_n_nloop = np.array(list(map(calculate_scale_n,
                                      scale,
                                      np.repeat(nveg, nloop),
                                      np.repeat(nsnow, nloop))))  # [nloop, n]

    return scale_n_nloop


class Forcings(object):
    ''' This class is a VIC forcing object

    Atributes
    ---------
    ds: <xarray.dataset>
        A dataset of VIC forcings
    lat_len: <int>
        Length of lat dimension
    lon_len: <int>
        Length of lon dimension
    time_len: <int>
        Length of time dimension
    
    Methods
    ---------
    '''
    
    def __init__(self, ds):
        self.ds = ds
        self.lat_len = len(ds['lat'])
        self.lon_len = len(ds['lon'])
        self.time_len = len(ds['time'])
        self.clean_up_time()
    
    
    def clean_up_time(self):
        ''' Clean up time variable'''
        
        self.ds['time'] = pd.to_datetime(self.ds['time'].values)
        
    
    def perturb_prec_lognormal(self, varname, std=1, phi=0, seed=None):
        ''' Perturb precipitation forcing data
        
        Parameters
        ----------
        varname: <str>
            Precipitation variable name in the forcing ds
        std: <float>
            Standard deviation of the multiplier (which is log-normal distributed);
            Note: here sigma is the real standard deviation of the multiplier, not
            the standard deviation of the underlying normal distribution! (and the
            real mean of the multiplier is 1)
        phi: <float>
            Parameter in AR(1) process. Default: 0 (no autocorrelation).
        seed: <int or None>
            Seed for random number generator; this seed will only be used locally
            in this function and will not affect the upper-level code.
            None for not re-assign seed in this function, but using the global seed)
            Default: None
        '''
        
        # --- Calculate mu and sigma for the lognormal distribution --- #
        # (here mu and sigma are mean and std of the underlying normal dist.)
        mu = -0.5 * np.log(np.square(std)+1)
        sigma = np.sqrt(np.log(np.square(std)+1))

        # Calculate std of white noise and generate random white noise
        scale = sigma * np.sqrt(1 - phi * phi)
        if seed is None:
            white_noise = np.random.normal(
                            loc=0, scale=scale,
                            size=(self.time_len, self.lat_len, self.lon_len))
        else:
            rng = np.random.RandomState(seed)
            white_noise = rng.normal(
                            loc=0, scale=scale,
                            size=(self.time_len, self.lat_len, self.lon_len))

        # --- AR(1) process --- #
        # Initialize
        ar1 = np.empty([self.time_len, self.lat_len, self.lon_len])
        # Generate data for the first time point (need to double check how to do this!!!!!)
        ar1[0, :, :] = white_noise[0, :, :]
        # Loop over each time point
        for t in range(1, self.time_len):
            ar1[t, :, :] = mu + phi * (ar1[t-1, :, :] - mu) +\
                              white_noise[t, :, :]

        # --- Calculate final noise by taking exp --- #
        noise = np.exp(ar1)
        
        # Add noise to soil moisture field
        ds_perturbed = self.ds.copy(deep=True)
        ds_perturbed[varname][:] *= noise
        
        return ds_perturbed

        
    def replace_prec(self, varname, da_prec):
        ''' Replace prec in the forcing by specified prec data
        
        Parameters
        ----------
        varname: <str>
            Precipitation variable name in the current forcing ds
        da_prec: <xr.DataArray>
            A DataArray of prec to be filled in.
            Must have the same dimensions as the forcing object itselt

        Returns
        ----------
        self.ds: <xr.Dataset>
            A dataset of forcings, with specified prec
        '''

        self.ds[varname][:] = da_prec.values

        return self.ds


class VarToPerturb(object):
    ''' This class is a variable to be perturbed

    Atributes
    ---------
    da: <xarray.DataArray>
        A DataArray of the variable to be perturbed
        Dimension: [time, lat, lon]

    Require
    ---------
    numpy
    '''

    def __init__(self, da):
        self.da = da  # dimension: [time, lat, lon]
        self.lat = self.da['lat']
        self.lon = self.da['lon']
        self.time = self.da['time']

    def add_gaussian_white_noise(self, da_sigma, da_max_values,
                                 adjust_negative=True, seed=None):
        ''' Add Gaussian noise for all active grid cells and all time steps

        Parameters
        ----------
        sigma: <xarray.DataArray>
            Standard deviation of the Gaussian white noise to add, can be spatially different
            for each grid cell (but temporally constant);
            Dimension: [lat, lon]
        da_max_values: <xarray.DataArray>
            Maximum values of variable for the whole domain. Perturbed values
            above maximum will be reset to maximum value.
            Dimension: [lat, lon]
        
        Returns
        ----------
        da_perturbed: <xarray.DataArray>
            Perturbed variable for the whole field
            Dimension: [time, lat, lon]
        adjust_negative: <bool>
            Whether or not to adjust negative variable values after
            adding noise to zero.
            Default: True (adjust negative to zero)
        seed: <int or None>
            Seed for random number generator; this seed will only be used locally
            in this function and will not affect the upper-level code.
            None for not re-assign seed in this function, but using the global seed)
            Default: None
        '''

        # Generate random noise for the whole field
        da_noise = self.da.copy(deep=True)
        da_noise[:] = np.nan
        for lt in self.lat:
            for lg in self.lon:
                sigma = da_sigma.loc[lt, lg].values
                if np.isnan(sigma) == True or sigma <= 0:  # if inactive cell, skip
                    continue
                if seed is None:
                    da_noise.loc[:, lt, lg] = np.random.normal(
                            loc=0, scale=sigma, size=len(self.time))
                else:
                    rng = np.random.RandomState(seed)
                    da_noise.loc[:, lt, lg] = rng.normal(
                            loc=0, scale=sigma, size=len(self.time))
        # Add noise to the original da
        da_perturbed = self.da + da_noise
        # Set negative to zero
        tmp = da_perturbed.values
        if adjust_negative:
            tmp[tmp<0] = 0
        # Set perturbed values above maximum to maximum values
        max_values = da_max_values.values  # [lat, lon]
        for i in range(len(self.lat)):
            for j in range(len(self.lon)):
                tmp[(tmp[:, i, j]>max_values[i, j]), i, j] = max_values[i, j]
        # Put into da
        da_perturbed[:] = tmp
        # Add attrs back
        da_perturbed.attrs = self.da.attrs

        return da_perturbed


def EnKF_VIC(N, start_time, end_time, init_state_nc, L, scale_n_nloop, da_max_moist_n,
             R, da_meas,
             da_meas_time_var, vic_exe, vic_global_template,
             ens_forcing_basedir, ens_forcing_prefix,
             vic_model_steps_per_day, output_vic_global_root_dir,
             output_vic_state_root_dir, output_vic_history_root_dir,
             output_vic_log_root_dir,
             nproc=1,
             mpi_proc=None, mpi_exe='mpiexec', debug=False, output_temp_dir=None,
             linear_model=False, linear_model_prec_varname=None,
             dict_linear_model_param=None):
    ''' This function runs ensemble kalman filter (EnKF) on VIC (image driver)

    Parameters
    ----------
    N: <int>
        Number of ensemble members
    start_time: <pandas.tslib.Timestamp>
        Start time of EnKF run
    end_time: <pandas.tslib.Timestamp>
        End time of EnKF run
    init_state_nc: <str>
        Initial state netCDF file
    L: <np.array>
        Cholesky decomposed matrix of covariance matrix P of all states:
                        P = L * L.T
        Thus, L * Z (where Z is i.i.d. standard normal random variables of
        dimension [n, n]) is multivariate normal random variables with
        mean zero and covariance matrix P.
        Dimension: [n, n]
    scale_n_nloop: <np.array>
        Standard deviation of noise to add for the whole field.
        Dimension: [nloop, n] (where nloop = lat * lon)
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    R: <np.array>  [m*m]
        Measurement error covariance matrix
    da_meas: <xr.DataArray> [time, lat, lon]
        DataArray of measurements (currently, must be only 1 variable of measurement);
        Measurements should already be truncated (if needed) so that they are all within the
        EnKF run period
    da_meas_time_var: <str>
        Time variable name in da_meas
    vic_exe: <class 'VIC'>
        VIC run class
    vic_global_template: <str>
        VIC global file template
    ens_forcing_basedir: <str>
        Ensemble forcing basedir ('ens_{}' subdirs should be under basedir)
    ens_forcing_prefix: <str>
        Prefix of ensemble forcing filenames under 'ens_{}' subdirs
        'YYYY.nc' will be appended
    vic_model_steps_per_day: <int>
        VIC model steps per day
    output_vic_global_root_dir: <str>
        Directory for VIC global files
    output_vic_state_root_dir: <str>
        Directory for VIC output state files
    output_vic_result_root_dir: <str>
        Directory for VIC output result files
    output_vic_log_root_dir: <str>
        Directory for VIC output log files
    nproc: <int>
        Number of processors to use
    mpi_proc: <int or None>
        Number of processors to use for VIC MPI run. None for not using MPI
    mpi_exe: <str>
        Path for MPI exe
    debug: <bool>
        True: output temp files for diagnostics; False: do not output temp files
    output_temp_dir: <str>
        Directory for temp files (for dignostic purpose); only used when
        debug = True
    linear_model: <bool>
        Whether to run a linear model instead of VIC for propagation.
        Default is 'False', which is to run VIC
        Note: some VIC input arguments will not be used if this option is True
    linear_model_prec_varname: <str>
        NOTE: this parameter is only needed if linear_model = True.
        Precip varname in the forcing netCDF files.
    dict_linear_model_param: <dict>
        NOTE: this parameter is only needed if linear_model = True.
        A dict of linear model parameters.
        Keys: 'r1', 'r2', 'r3', 'r12', 'r23'

    Required
    ----------
    numpy
    pandas
    os

    '''

    # --- Pre-processing and checking inputs ---#
    m = 1  # number of measurements
    n_time = len(da_meas[da_meas_time_var])  # number of measurement time points
    # Determine fraction of each veg/snowband tile in each grid cell
    if not linear_model:
        da_tile_frac = determine_tile_frac(vic_global_template)
        adjust_negative = True
    else:
        da_tile_frac = xr.DataArray(
                np.ones([1, 1, len(da_meas['lat']), len(da_meas['lon'])]),
                coords=[[1], [0], da_meas['lat'], da_meas['lon']],
                dims=['veg_class', 'snow_band', 'lat', 'lon'])
        adjust_negative = False
    
    # Check whether the run period is consistent with VIC setup
    pass
    # Check whether the time range of da_meas is within run period
    pass
   
    # --- Setup subdirectories --- #
    out_hist_concat_dir = setup_output_dirs(
                output_vic_history_root_dir,
                mkdirs=['EnKF_ensemble_concat'])['EnKF_ensemble_concat']
 
    # --- Step 1. Initialize ---#
    init_state_time = start_time
    print('\tGenerating ensemble initial states at ', init_state_time)
    time1 = timeit.default_timer()
    # Load initial state file
    ds_states = xr.open_dataset(init_state_nc)
    class_states = States(ds_states)
    
    # Determine the number of EnKF states, n
    n = len(class_states.da_EnKF['n'])
    # Set up initial state subdirectories
    init_state_dir_name = 'init.{}_{:05d}'.format(
                                    init_state_time.strftime('%Y%m%d'),
                                    init_state_time.hour*3600+init_state_time.second)
    init_state_dir = setup_output_dirs(
                            output_vic_state_root_dir,
                            mkdirs=[init_state_dir_name])[init_state_dir_name]
    # For each ensemble member, add Gaussian noise to sm states with covariance P,
    # and save each ensemble member states
    if debug:
        debug_dir = setup_output_dirs(
                        output_temp_dir,
                        mkdirs=[init_state_dir_name])[init_state_dir_name]
    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        for i in range(N):
            seed = np.random.randint(low=100000)
            da_perturbation = perturb_soil_moisture_states_class_input(
                    class_states=class_states,
                    L=L,
                    scale_n_nloop=scale_n_nloop,
                    out_states_nc=os.path.join(init_state_dir,
                                               'state.ens{}.nc'.format(i+1)),
                    da_max_moist_n=da_max_moist_n,
                    adjust_negative=adjust_negative,
                    seed=seed)
            # Save soil moisture perturbation
            if debug:
                ds_perturbation = xr.Dataset({'STATE_SOIL_MOISTURE':
                                             (ds - class_states.ds)\
                                  ['STATE_SOIL_MOISTURE']})
                ds_perturbation.to_netcdf(os.path.join(
                        debug_dir,
                        'perturbation.ens{}.nc').format(i+1))
    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            seed = np.random.randint(low=100000)
            pool.apply_async(
                perturb_soil_moisture_states_class_input,
                (class_states, L, scale_n_nloop,
                 os.path.join(init_state_dir, 'state.ens{}.nc'.format(i+1)),
                 da_max_moist_n, adjust_negative, seed))
#            # Save soil moisture perturbation
#            if debug:
#                ds_perturbation = xr.Dataset({'STATE_SOIL_MOISTURE':
#                                             (ds - class_states.ds)\
#                                  ['STATE_SOIL_MOISTURE']})
#                ds_perturbation.to_netcdf(os.path.join(
#                        debug_dir,
#                        'perturbation.ens{}.nc').format(i+1))
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()

    time2 = timeit.default_timer()
    print('\t\tTime of perturbing init state: {}'.format(time2-time1))

    # --- Step 2. Propagate (run VIC) until the first measurement time point ---#    
    # Initialize dictionary of history file paths for each ensemble member
    dict_ens_list_history_files = {}
    for i in range(N):
        dict_ens_list_history_files['ens{}'.format(i+1)] = []

    # Determine VIC run period
    vic_run_start_time = start_time
    vic_run_end_time = pd.to_datetime(da_meas[da_meas_time_var].values[0]) - \
                       pd.DateOffset(hours=24/vic_model_steps_per_day)
    print('\tPropagating (run VIC) until the first measurement time point ',
          pd.to_datetime(da_meas[da_meas_time_var].values[0]))
    time1 = timeit.default_timer()
    # Set up output states, history and global files directories
    propagate_output_dir_name = 'propagate.{}_{:05d}-{}'.format(
                        vic_run_start_time.strftime('%Y%m%d'),
                        vic_run_start_time.hour*3600+vic_run_start_time.second,
                        vic_run_end_time.strftime('%Y%m%d'))
    out_state_dir = setup_output_dirs(
                            output_vic_state_root_dir,
                            mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
    out_history_dir = setup_output_dirs(
                            output_vic_history_root_dir,
                            mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
    out_global_dir = setup_output_dirs(
                            output_vic_global_root_dir,
                            mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
    out_log_dir = setup_output_dirs(
                            output_vic_log_root_dir,
                            mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
    # Propagate all ensemble members
    if not linear_model:
        propagate_ensemble(
                N,
                start_time=vic_run_start_time,
                end_time=vic_run_end_time,
                vic_exe=vic_exe,
                vic_global_template_file=vic_global_template,
                vic_model_steps_per_day=vic_model_steps_per_day,
                init_state_dir=init_state_dir,
                out_state_dir=out_state_dir,
                out_history_dir=out_history_dir,
                out_global_dir=out_global_dir,
                out_log_dir=out_log_dir,
                ens_forcing_basedir=ens_forcing_basedir,
                ens_forcing_prefix=ens_forcing_prefix,
                nproc=nproc,
                mpi_proc=mpi_proc,
                mpi_exe=mpi_exe)
    else:
        propagate_ensemble_linear_model(
                N, 
                start_time=vic_run_start_time,
                end_time=vic_run_end_time,
                lat_coord=da_meas['lat'],
                lon_coord=da_meas['lon'],
                model_steps_per_day=vic_model_steps_per_day,
                init_state_dir=init_state_dir,
                out_state_dir=out_state_dir,
                out_history_dir=out_history_dir,
                ens_forcing_basedir=ens_forcing_basedir,
                ens_forcing_prefix=ens_forcing_prefix,
                prec_varname=linear_model_prec_varname,
                dict_linear_model_param=dict_linear_model_param,
                nproc=nproc)
    # Clean up log dir
    shutil.rmtree(out_log_dir)

    # Put output history file paths into dictionary
    for i in range(N):
        dict_ens_list_history_files['ens{}'.format(i+1)].append(os.path.join(
                    out_history_dir, 'history.ens{}.{}-{:05d}.nc'.format(
                            i+1,
                            vic_run_start_time.strftime('%Y-%m-%d'),
                            vic_run_start_time.hour*3600+vic_run_start_time.second)))
    time2 = timeit.default_timer()
    print('\t\tTime of propagation: {}'.format(time2-time1))
    
    # --- Step 3. Run EnKF --- #
    debug_innov_dir = setup_output_dirs(
                    output_temp_dir,
                    mkdirs=['innov'])['innov']
    if debug:
        debug_perturbation_dir = setup_output_dirs(
                            output_temp_dir,
                            mkdirs=['perturbation'])['perturbation']
        debug_update_dir = setup_output_dirs(
                        output_temp_dir,
                        mkdirs=['update'])['update']
    # Initialize
    state_dir_after_prop = out_state_dir

    # Loop over each measurement time point
    for t, time in enumerate(da_meas[da_meas_time_var]):

        # Determine last, current and next measurement time points
        current_time = pd.to_datetime(time.values)
        if t == len(da_meas[da_meas_time_var])-1:  # if this is the last measurement time
            next_time = end_time
        else:  # if not the last measurement time
            next_time = pd.to_datetime(da_meas[da_meas_time_var][t+1].values) - \
                        pd.DateOffset(hours=24/vic_model_steps_per_day)
        print('\tCalculating for ', current_time, 'to', next_time)

        # (1.1) Calculate gain K
        time1 = timeit.default_timer()
        da_x, da_y_est = get_soil_moisture_and_estimated_meas_all_ensemble(
                                N,
                                state_dir=state_dir_after_prop,
                                state_time=current_time,
                                da_tile_frac=da_tile_frac,
                                nproc=nproc)
        da_K = calculate_gain_K_whole_field(da_x, da_y_est, R)
        if debug:
            ds_K = xr.Dataset({'K': da_K})
            ds_K.to_netcdf(os.path.join(debug_update_dir,
                                        'K.{}_{:05d}.nc'.format(
                                            current_time.strftime('%Y%m%d'),
                                            current_time.hour*3600+current_time.second)))
        time2 = timeit.default_timer()
        print('\t\tTime of calculating gain K: {}'.format(time2-time1))

        # (1.2) Calculate and save normalized innovation
        time1 = timeit.default_timer()
        # Calculate ensemble mean of y_est
        da_y_est_ensMean = da_y_est.mean(dim='N')  # [lat, lon, m]
        # Calculate non-normalized innovation
        innov = da_meas.loc[time, :, :, :].values - \
                da_y_est_ensMean.values  # [lat, lon, m]
        da_innov = xr.DataArray(innov, coords=[da_y_est_ensMean['lat'],
                                               da_y_est_ensMean['lon'],
                                               da_y_est_ensMean['m']],
                                dims=['lat', 'lon', 'm'])
        # Normalize innovation
        da_Pyy = da_y_est.var(dim='N', ddof=1)  # [lat, lon, m]
        innov_norm = innov / np.sqrt(da_Pyy.values + R)  # [lat, lon, m]
        da_innov_norm = xr.DataArray(innov_norm,
                                     coords=[da_y_est_ensMean['lat'],
                                             da_y_est_ensMean['lon'],
                                             da_y_est_ensMean['m']],
                                     dims=['lat', 'lon', 'm'])
        # Save normalized innovation to netCDf file
        ds_innov_norm = xr.Dataset({'innov_norm': da_innov_norm})
        ds_innov_norm.to_netcdf(os.path.join(
                debug_innov_dir,
                'innov_norm.{}_{:05d}.nc'.format(
                        current_time.strftime('%Y%m%d'),
                        current_time.hour*3600+current_time.second)))
        time2 = timeit.default_timer()
        print('\t\tTime of calculating innovation: {}'.format(time2-time1))

        # (1.3) Update states for each ensemble member
        # Set up dir for updated states
        time1 = timeit.default_timer()
        updated_states_dir_name = 'updated.{}_{:05d}'.format(
                            current_time.strftime('%Y%m%d'),
                            current_time.hour*3600+current_time.second)
        out_updated_state_dir = setup_output_dirs(
                output_vic_state_root_dir,
                mkdirs=[updated_states_dir_name])[updated_states_dir_name]
        # Update states and save to nc files
        da_x_updated, da_update_increm, da_v = update_states_ensemble(
                da_y_est, da_K,
                da_meas.loc[time, :, :, :],
                R,
                state_dir_before_update=state_dir_after_prop,
                state_time=current_time,
                out_vic_state_dir=out_updated_state_dir,
                da_max_moist_n=da_max_moist_n,
                adjust_negative=adjust_negative,
                nproc=nproc)
        if debug:
            # Save update increment to netCDF file
            ds_update_increm = xr.Dataset({'update_increment': da_update_increm})
            ds_update_increm.to_netcdf(os.path.join(
                    debug_update_dir,
                    'update_increm.{}_{:05d}.nc'.format(
                             current_time.strftime('%Y%m%d'),
                             current_time.hour*3600+current_time.second)))
            # Save measurement perturbation in the update step to netCDF file
            ds_v = xr.Dataset({'meas_perturbation': da_v})
            ds_v.to_netcdf(os.path.join(
                    debug_update_dir,
                    'meas_perturbation.{}_{:05d}.nc'.format(
                             current_time.strftime('%Y%m%d'),
                             current_time.hour*3600+current_time.second)))
        # Delete propagated states
        shutil.rmtree(state_dir_after_prop)
        time2 = timeit.default_timer()
        print('\t\tTime of updating states: {}'.format(time2-time1))

        # (2) Perturb states
        time1 = timeit.default_timer()
        # Set up perturbed state subdirectories
        pert_state_dir_name = 'perturbed.{}_{:05d}'.format(
                                        current_time.strftime('%Y%m%d'),
                                        current_time.hour*3600+current_time.second)
        pert_state_dir = setup_output_dirs(
                            output_vic_state_root_dir,
                            mkdirs=[pert_state_dir_name])[pert_state_dir_name]
        # Perturb states for each ensemble member
        list_da_perturbation = perturb_soil_moisture_states_ensemble(
                    N,
                    states_to_perturb_dir=out_updated_state_dir,
                    L=L,
                    scale_n_nloop=scale_n_nloop,
                    out_states_dir=pert_state_dir,
                    da_max_moist_n=da_max_moist_n,
                    adjust_negative=adjust_negative,
                    nproc=nproc)
        if debug:
            da_perturbation = xr.concat(list_da_perturbation, dim='N')
            da_perturbation['N'] = range(1, N+1)
            ds_perturbation = xr.Dataset({'soil_moisture_perturbation':
                                          da_perturbation})
            ds_perturbation.to_netcdf(os.path.join(
                        debug_perturbation_dir,
                        'perturbation.{}_{:05d}.nc').format(
                                current_time.strftime('%Y%m%d'),
                                current_time.hour*3600+current_time.second))
        time2 = timeit.default_timer()
        print('\t\tTime of perturbing states: {}'.format(time2-time1))

        # (3) Propagate each ensemble member to the next measurement time point
        # If current_time > next_time, do not propagate (we already reach the end of the simulation)
        if current_time > next_time:
            break
        # --- Propagate to the next time point --- #
        time1 = timeit.default_timer()
        propagate_output_dir_name = 'propagate.{}_{:05d}-{}'.format(
                                            current_time.strftime('%Y%m%d'),
                                            current_time.hour*3600+current_time.second,
                                            next_time.strftime('%Y%m%d'))
        out_state_dir = setup_output_dirs(
                                output_vic_state_root_dir,
                                mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
        out_history_dir = setup_output_dirs(
                                output_vic_history_root_dir,
                                mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
        out_global_dir = setup_output_dirs(
                                output_vic_global_root_dir,
                                mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
        out_log_dir = setup_output_dirs(
                                output_vic_log_root_dir,
                                mkdirs=[propagate_output_dir_name])[propagate_output_dir_name]
        if not linear_model:
            propagate_ensemble(
                    N, start_time=current_time, end_time=next_time,
                    vic_exe=vic_exe,
                    vic_global_template_file=vic_global_template,
                    vic_model_steps_per_day=vic_model_steps_per_day,
                    init_state_dir=pert_state_dir,  # perturbed states as init state
                    out_state_dir=out_state_dir,
                    out_history_dir=out_history_dir,
                    out_global_dir=out_global_dir,
                    out_log_dir=out_log_dir,
                    ens_forcing_basedir=ens_forcing_basedir,
                    ens_forcing_prefix=ens_forcing_prefix,
                    nproc=nproc,
                    mpi_proc=mpi_proc,
                    mpi_exe=mpi_exe)
        else:
            propagate_ensemble_linear_model(
                    N, 
                    start_time=current_time,
                    end_time=next_time,
                    lat_coord=da_meas['lat'],
                    lon_coord=da_meas['lon'],
                    model_steps_per_day=vic_model_steps_per_day,
                    init_state_dir=pert_state_dir,  # perturbed states as init state
                    out_state_dir=out_state_dir,
                    out_history_dir=out_history_dir,
                    ens_forcing_basedir=ens_forcing_basedir,
                    ens_forcing_prefix=ens_forcing_prefix,
                    prec_varname=linear_model_prec_varname,
                    dict_linear_model_param=dict_linear_model_param,
                    nproc=nproc)
        # Clean up log dir
        shutil.rmtree(out_log_dir)

        # Put output history file paths into dictionary
        for i in range(N):
            dict_ens_list_history_files['ens{}'.format(i+1)].append(os.path.join(
                    out_history_dir, 'history.ens{}.{}-{:05d}.nc'.format(
                            i+1,
                            current_time.strftime('%Y-%m-%d'),
                            current_time.hour*3600+current_time.second)))
        # Delete perturbed states
        shutil.rmtree(pert_state_dir)
        time2 = timeit.default_timer()
        print('\t\tTime of propagation: {}'.format(time2-time1))
        
        # Point state directory to be updated to the propagated one
        state_dir_after_prop = out_state_dir

        # --- Concat and delete individual history files for each year --- #
        # (If the end of EnKF run, or the end of a calendar year)
        if (t == (len(da_meas[da_meas_time_var]) - 1)) or \
           (t < (len(da_meas[da_meas_time_var]) - 1) and \
           current_time.year != (next_time + \
           pd.DateOffset(hours=24/vic_model_steps_per_day)).year):
            print('\tConcatenating history files...')
            time1 = timeit.default_timer()
            # Determine history file year
            year = current_time.year
            # Identify history dirs to delete later
            list_dir_to_delete = []
            for f in dict_ens_list_history_files['ens1']:
                list_dir_to_delete.append(os.path.dirname(f))
            set_dir_to_delete = set(list_dir_to_delete)  # remove duplicated dirs
            # --- If nproc == 1, do a regular ensemble loop --- #
            if nproc == 1:
                # Concat for each ensemble member and delete individual files
                for i in range(N):
                    # Concat and clean up
                    list_history_files=dict_ens_list_history_files\
                                       ['ens{}'.format(i+1)]
                    output_file=os.path.join(
                                    out_hist_concat_dir,
                                    'history.ens{}.concat.{}.nc'.format(
                                        i+1, year))
                    concat_clean_up_history_file(list_history_files,
                                                 output_file)
                    # Reset history file list
                    dict_ens_list_history_files['ens{}'.format(i+1)] = []
            # --- If nproc > 1, use multiprocessing --- #
            elif nproc > 1:
                # Set up multiprocessing
                pool = mp.Pool(processes=nproc)
                # Loop over each ensemble member
                for i in range(N):
                    # Concat and clean up
                    list_history_files=dict_ens_list_history_files\
                                       ['ens{}'.format(i+1)]
                    output_file=os.path.join(
                                    out_hist_concat_dir,
                                    'history.ens{}.concat.{}.nc'.format(
                                        i+1, year))
                    pool.apply_async(concat_clean_up_history_file,
                                     (list_history_files, output_file))
                    # Reset history file list
                    dict_ens_list_history_files['ens{}'.format(i+1)] = []
                # --- Finish multiprocessing --- #
                pool.close()
                pool.join()
            time2 = timeit.default_timer()
            print('\t\tTime of propagation: {}'.format(time2-time1))

            # Delete history dirs containing individual files
            time1 = timeit.default_timer()
            for d in set_dir_to_delete:
                shutil.rmtree(d)
            time2 = timeit.default_timer()
            print('\t\tTime of deleting history directories: {}'.format(time2-time1))

    # --- Concat and clean up normalized innovation results --- #
    debug_innov_dir
    time1 = timeit.default_timer()
    print('\tInnovation...')
    list_da = []
    list_file_to_delete = []
    times = da_meas[da_meas_time_var].values
    for time in times:
        t = pd.to_datetime(time)
        # Load data
        fname = '{}/innov_norm.{}_{:05d}.nc'.format(
                    debug_innov_dir, t.strftime('%Y%m%d'),
                    t.hour*3600+t.second)
        da = xr.open_dataset(fname)['innov_norm'].sel(m=1)  # [lat, lon]
        # Put data in array
        list_da.append(da)
        # Add individual file to list to delete
        list_file_to_delete.append(fname)
    # Concat innovation of all times
    da_innov_norm = xr.concat(list_da, dim='time')
    da_innov_norm['time'] = da_meas[da_meas_time_var].values
    # Write to file
    ds_innov_norm = xr.Dataset({'innov_norm': da_innov_norm})
    ds_innov_norm.to_netcdf(
        os.path.join(
            debug_innov_dir,
            'innov_norm.concat.{}_{}.nc'.format(
                    pd.to_datetime(times[0]).year,
                    pd.to_datetime(times[-1]).year)),
        format='NETCDF4_CLASSIC')
    # Delete individule files
    for f in list_file_to_delete:
        os.remove(f)
    time2 = timeit.default_timer()
    print('Time of concatenating innovation: {}'.format(time2-time1))

    # --- Concat and clean up debugging results --- #
    if debug:
        # --- Perturbation --- #
        time1 = timeit.default_timer()
        print('\tConcatenating debugging results - perturbation...')
        list_da = []
        list_file_to_delete = []
        times = da_meas[da_meas_time_var].values
        for time in times:
            t = pd.to_datetime(time)
            # Load data
            fname = '{}/perturbation.{}_{:05d}.nc'.format(
                        debug_perturbation_dir, t.strftime('%Y%m%d'),
                        t.hour*3600+t.second)
            da = xr.open_dataset(fname)['soil_moisture_perturbation']
            # Put data in array
            list_da.append(da)
            # Add individual file to list to delete
            list_file_to_delete.append(fname)
        # Concat all times
        da_concat = xr.concat(list_da, dim='time')
        da_concat['time'] = da_meas[da_meas_time_var].values
        # Write to file
        ds_concat = xr.Dataset({'soil_moisture_perturbation': da_concat})
        ds_concat.to_netcdf(
            os.path.join(
                debug_perturbation_dir,
                'perturbation.concat.{}_{}.nc'.format(
                        pd.to_datetime(times[0]).year,
                        pd.to_datetime(times[-1]).year)),
            format='NETCDF4_CLASSIC')
        # Delete individule files
        for f in list_file_to_delete:
            os.remove(f)
        time2 = timeit.default_timer()
        print('Time of concatenating perturbation: {}'.format(time2-time1))

        # --- Update increment --- #
        time1 = timeit.default_timer()
        print('\tConcatenating debugging results - update increment...')
        list_da = []
        list_file_to_delete = []
        times = da_meas[da_meas_time_var].values
        for time in times:
            t = pd.to_datetime(time)
            # Load data
            fname = '{}/update_increm.{}_{:05d}.nc'.format(
                        debug_update_dir, t.strftime('%Y%m%d'),
                        t.hour*3600+t.second)
            da = xr.open_dataset(fname)['update_increment']
            # Put data in array
            list_da.append(da)
            # Add individual file to list to delete
            list_file_to_delete.append(fname)
        # Concat all times
        da_concat = xr.concat(list_da, dim='time')
        da_concat['time'] = da_meas[da_meas_time_var].values
        # Write to file
        ds_concat = xr.Dataset({'update_increment': da_concat})
        ds_concat.to_netcdf(
            os.path.join(
                debug_update_dir,
                'update_increment.concat.{}_{}.nc'.format(
                        pd.to_datetime(times[0]).year,
                        pd.to_datetime(times[-1]).year)),
            format='NETCDF4_CLASSIC')
        # Delete individule files
        for f in list_file_to_delete:
            os.remove(f)
        time2 = timeit.default_timer()
        print('Time of concatenating update increment: {}'.format(time2-time1))

        # --- Gain K --- #
        time1 = timeit.default_timer()
        print('\tConcatenating debugging results - gain K...')
        list_da = []
        list_file_to_delete = []
        times = da_meas[da_meas_time_var].values
        for time in times:
            t = pd.to_datetime(time)
            # Load data
            fname = '{}/K.{}_{:05d}.nc'.format(
                        debug_update_dir, t.strftime('%Y%m%d'),
                        t.hour*3600+t.second)
            da = xr.open_dataset(fname)['K']
            # Put data in array
            list_da.append(da)
            # Add individual file to list to delete
            list_file_to_delete.append(fname)
        # Concat all times
        da_concat = xr.concat(list_da, dim='time')
        da_concat['time'] = da_meas[da_meas_time_var].values
        # Write to file
        ds_concat = xr.Dataset({'K': da_concat})
        ds_concat.to_netcdf(
            os.path.join(
                debug_update_dir,
                'K.concat.{}_{}.nc'.format(
                        pd.to_datetime(times[0]).year,
                        pd.to_datetime(times[-1]).year)),
            format='NETCDF4_CLASSIC')
        # Delete individule files
        for f in list_file_to_delete:
            os.remove(f)
        time2 = timeit.default_timer()
        print('Time of concatenating gain K: {}'.format(time2-time1))

        # --- Measurement perturbation --- #
        time1 = timeit.default_timer()
        print('\tConcatenating debugging results - meas. perturbation...')
        list_da = []
        list_file_to_delete = []
        times = da_meas[da_meas_time_var].values
        for time in times:
            t = pd.to_datetime(time)
            # Load data
            fname = '{}/meas_perturbation.{}_{:05d}.nc'.format(
                        debug_update_dir, t.strftime('%Y%m%d'),
                        t.hour*3600+t.second)
            da = xr.open_dataset(fname)['meas_perturbation']
            # Put data in array
            list_da.append(da)
            # Add individual file to list to delete
            list_file_to_delete.append(fname)
        # Concat all times
        da_concat = xr.concat(list_da, dim='time')
        da_concat['time'] = da_meas[da_meas_time_var].values
        # Write to file
        ds_concat = xr.Dataset({'meas_perturbation': da_concat})
        ds_concat.to_netcdf(
            os.path.join(
                debug_update_dir,
                'meas_perturbation.concat.{}_{}.nc'.format(
                        pd.to_datetime(times[0]).year,
                        pd.to_datetime(times[-1]).year)),
            format='NETCDF4_CLASSIC')
        # Delete individule files
        for f in list_file_to_delete:
            os.remove(f)
        time2 = timeit.default_timer()
        print('Time of concatenating gain meas perturbation: {}'.format(time2-time1))


def to_netcdf_history_file_compress(ds_hist, out_nc):
    ''' This function saves a VIC-history-file-format ds to netCDF, with
        compression.

    Parameters
    ----------
    ds_hist: <xr.Dataset>
        History dataset to save
    out_nc: <str>
        Path of output netCDF file
    '''

    dict_encode = {}
    for var in ds_hist.data_vars:
        # skip variables not starting with "OUT_"
        if var.split('_')[0] != 'OUT':
            continue
        # determine chunksizes
        chunksizes = []
        for i, dim in enumerate(ds_hist[var].dims):
            if dim == 'time':  # for time dimension, chunksize = 1
                chunksizes.append(1)
            else:
                chunksizes.append(len(ds_hist[dim]))
        # create encoding dict
        dict_encode[var] = {'zlib': True,
                            'complevel': 1,
                            'chunksizes': chunksizes}
    ds_hist.to_netcdf(out_nc,
                      format='NETCDF4',
                      encoding=dict_encode)


def to_netcdf_state_file_compress(ds_state, out_nc):
    ''' This function saves a VIC-state-file-format ds to netCDF, with
        compression.

    Parameters
    ----------
    ds_state: <xr.Dataset>
        State dataset to save
    out_nc: <str>
        Path of output netCDF file
    '''

    dict_encode = {}
    for var in ds_state.data_vars:
        if var.split('_')[0] != 'STATE':
            continue
        # create encoding dict
        dict_encode[var] = {'zlib': True,
                            'complevel': 1}
    ds_state.to_netcdf(out_nc,
                       format='NETCDF4',
                       encoding=dict_encode)


def to_netcdf_forcing_file_compress(ds_force, out_nc):
    ''' This function saves a VIC-forcing-file-format ds to netCDF, with
        compression.

    Parameters
    ----------
    ds_force: <xr.Dataset>
        Forcing dataset to save
    out_nc: <str>
        Path of output netCDF file
    '''

    dict_encode = {}
    for var in ds_force.data_vars:
        # determine chunksizes
        chunksizes = []
        for i, dim in enumerate(ds_force[var].dims):
            if dim == 'time':  # for time dimension, chunksize = 1
                chunksizes.append(1)
            else:
                chunksizes.append(len(ds_force[dim]))
        # create encoding dict
        dict_encode[var] = {'zlib': True,
                            'complevel': 1,
                            'chunksizes': chunksizes}
    ds_force.to_netcdf(out_nc,
                      format='NETCDF4',
                      encoding=dict_encode)


def concat_clean_up_history_file(list_history_files, output_file):
    ''' This function is for wrapping up history file concat and clean up
        for the use of multiprocessing package; history file output is
        compressed.
    
    Parameters
    ----------
    list_history_files: <list>
        A list of output history files (in order) to concat and delete
    output_file: <str>
        Filename for output concatenated netCDF file

    Requires
    ----------
    xarray
    os
    concat_vic_history_files
    '''

    # --- Concat --- #
    ds_concat = concat_vic_history_files(list_history_files)
    # --- Save concatenated file to netCDF (with compression) --- #
    to_netcdf_history_file_compress(ds_concat, output_file)
    # Clean up individual history files
    for f in list_history_files:
        os.remove(f)


def generate_VIC_global_file(global_template_path, model_steps_per_day,
                             start_time, end_time, init_state, vic_state_basepath,
                             vic_history_file_dir, replace,
                             output_global_basepath):
    ''' This function generates a VIC global file from a template file.
    
    Parameters
    ----------
    global_template_path: <str>
        VIC global parameter template (some parts to be filled in)
    model_steps_per_day: <int>
        VIC model steps per day for model run, runoff run and output
    start_time: <pandas.tslib.Timestamp>
        Model run start time
    end_time: <pandas.tslib.Timestamp>
        Model run end time (the beginning of the last time step)
    init_state: <str>
        A full line of initial state option in the global file.
        E.g., "# INIT_STATE"  for no initial state;
              or "INIT_STATE /path/filename" for an initial state file
    vic_state_basepath: <str>
        Output state name directory and file name prefix.
        None if do not want to output state file.
    vic_history_file_dir: <str>
        Output history file directory
    replace: <collections.OrderedDict>
        An ordered dictionary of globap parameters to be replaced
    output_global_basepath: <str>
        Output global file basepath
        ".<start_time>_<end_date>.nc" will be appended,
            where <start_time> is in '%Y%m%d-%H%S',
                  <end_date> is in '%Y%m%d' (since VIC always runs until the end of a date)
    
    Returns
    ----------
    output_global_file: <str>
        VIC global file path
    
    Require
    ----------
    string
    OrderedDict
    pandas
    '''
    
    # --- Create template string --- #
    with open(global_template_path, 'r') as global_file:
        global_param = global_file.read()

    s = string.Template(global_param)
    
    # --- Fill in global parameter options --- #
    state_time = end_time + pd.DateOffset(days=1/model_steps_per_day)

    global_param = s.safe_substitute(model_steps_per_day=model_steps_per_day,
                                     startyear=start_time.year,
                                     startmonth=start_time.month,
                                     startday=start_time.day,
                                     startsec=start_time.hour*3600+start_time.second,
                                     endyear=end_time.year,
                                     endmonth=end_time.month,
                                     endday=end_time.day,
                                     init_state=init_state,
                                     statename=vic_state_basepath,
                                     stateyear=state_time.year, # save state at the end of end_time time step (end_time is the beginning of that time step)
                                     statemonth=state_time.month,
                                     stateday=state_time.day,
                                     statesec=state_time.hour*3600+state_time.second,
                                     result_dir=vic_history_file_dir)
    
    # --- Replace global parameters in replace --- #
    global_param = replace_global_values(global_param, replace)

    # --- If vic_state_basepath == None, add "#" in front of STATENAME --- #
    if vic_state_basepath is None:
        for i, line in enumerate(global_param):
            if line.split()[0] == 'STATENAME':
                global_param[i] = "# STATENAME"
    
    # --- Write global parameter file --- #
    output_global_file = '{}.{}_{}.txt'.format(
                                output_global_basepath,
                                start_time.strftime('%Y%m%d-%H%S'),
                                end_time.strftime('%Y%m%d'))
    
    with open(output_global_file, mode='w') as f:
        for line in global_param:
            f.write(line)

    return output_global_file


def setup_output_dirs(out_basedir, mkdirs=['results', 'state',
                                            'logs', 'plots']):
    ''' This function creates output directories.
    
    Parameters
    ----------
    out_basedir: <str>
        Output base directory for all output files
    mkdirs: <list>
        A list of subdirectories to make

    Require
    ----------
    os
    OrderedDict
    
    Returns
    ----------
    dirs: <OrderedDict>
        A dictionary of subdirectories
    
    '''

    dirs = OrderedDict()
    for d in mkdirs:
        dirs[d] = os.path.join(out_basedir, d)

    for dirname in dirs.values():
        os.makedirs(dirname, exist_ok=True)

    return dirs


def check_returncode(returncode, expected=0):
    '''check return code given by VIC, raise error if appropriate
    
    Require
    ---------
    tonic.models.vic.vic.default_vic_valgrind_error_code
    class VICReturnCodeError
    '''
    if returncode == expected:
        return None
    elif returncode == default_vic_valgrind_error_code:
        raise VICValgrindError('Valgrind raised an error')
    else:
        raise VICReturnCodeError('VIC return code ({0}) does not match '
                                 'expected ({1})'.format(returncode, expected))


def run_vic_for_multiprocess(vic_exe, global_file, log_dir,
                             mpi_proc=None, mpi_exe=None):
    '''This function is a simple wrapper for calling "run" method under
        VIC class in multiprocessing

    Parameters
    ----------
    vic_exe: <str>
        A VIC class object
    global_file: <str>
        VIC global file path
    log_dir: <str>
        VIC run output log directory
    mpi_proc: <int or None>
        Number of processors to use for VIC MPI run. None for not using MPI
    mpi_exe: <str>
        Path for MPI exe. Only used if mpi_proc is not None

    Require
    ----------
    check_returncode
    '''

    if mpi_proc == None:
        returncode = vic_exe.run(global_file, logdir=log_dir,
                                 **{'mpi_proc': mpi_proc})
        check_returncode(returncode, expected=0)
    else:
        returncode = vic_exe.run(global_file, logdir=log_dir,
                                 **{'mpi_proc': mpi_proc, 'mpi_exe': mpi_exe})
        check_returncode(returncode, expected=0)


def propagate_ensemble(N, start_time, end_time, vic_exe, vic_global_template_file,
                       vic_model_steps_per_day, init_state_dir, out_state_dir,
                       out_history_dir, out_global_dir, out_log_dir,
                       ens_forcing_basedir, ens_forcing_prefix, nproc=1,
                       mpi_proc=None, mpi_exe='mpiexec'):
    ''' This function propagates (via VIC) an ensemble of states to a certain time point.
    
    Parameters
    ----------
    N: <int>
        Number of ensemble members
    start_time: <pandas.tslib.Timestamp>
        Start time of this propagation run
    end_time: <pandas.tslib.Timestamp>
        End time of this propagation
    vic_exe: <class 'VIC'>
        Tonic VIC class
    vic_global_template_file: <str>
        Path of VIC global file template
    vic_model_steps_per_day: <str>
        VIC option - model steps per day
    init_state_dir: <str>
        Directory of initial states for each ensemble member
        State file names are "state.ens<i>", where <i> is 1, 2, ..., N
    out_state_dir: <str>
        Directory of output states for each ensemble member
        State file names will be "state.ens<i>.xxx.nc", where <i> is 1, 2, ..., N
    out_history_dir: <str>
        Directory of output history files for each ensemble member
        History file names will be "history.ens<i>.nc", where <i> is 1, 2, ..., N
    out_global_dir: <str>
        Directory of output global files for each ensemble member
        Global file names will be "global.ens<i>.txt", where <i> is 1, 2, ..., N
    out_log_dir: <str>
        Directory of output log files for each ensemble member
        Log file names will be "global.ens<i>.xxx", where <i> is 1, 2, ..., N
    ens_forcing_basedir: <str>
        Ensemble forcing basedir ('ens_{}' subdirs should be under basedir)
    ens_forcing_prefix: <str>
        Prefix of ensemble forcing filenames under 'ens_{}' subdirs
        'YYYY.nc' will be appended
    nproc: <int>
        Number of processors to use for parallel ensemble
        Default: 1
    mpi_proc: <int or None>
        Number of processors to use for VIC MPI run. None for not using MPI
        Default: None
    mpi_exe: <str>
        Path for MPI exe. Only used if mpi_proc is not None
        
    Require
    ----------
    OrderedDict
    multiprocessing
    generate_VIC_global_file
    '''

    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        for i in range(N):
            # Generate VIC global param file
            replace = OrderedDict([('FORCING1', os.path.join(
                                            ens_forcing_basedir,
                                            'ens_{}'.format(i+1),
                                            ens_forcing_prefix)),
                                   ('OUTFILE', 'history.ens{}'.format(i+1))])
            global_file = generate_VIC_global_file(
                                global_template_path=vic_global_template_file,
                                model_steps_per_day=vic_model_steps_per_day,
                                start_time=start_time,
                                end_time=end_time,
                                init_state="INIT_STATE {}".format(
                                        os.path.join(
                                            init_state_dir,
                                            'state.ens{}.nc'.format(i+1))),
                                vic_state_basepath=os.path.join(
                                            out_state_dir,
                                            'state.ens{}'.format(i+1)),
                                vic_history_file_dir=out_history_dir,
                                replace=replace,
                                output_global_basepath=os.path.join(
                                            out_global_dir,
                                            'global.ens{}'.format(i+1)))
            # Run VIC
            run_vic_for_multiprocess(vic_exe, global_file, out_log_dir,
                                     mpi_proc, mpi_exe)
    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            # Generate VIC global param file
            replace = OrderedDict([('FORCING1', os.path.join(
                                            ens_forcing_basedir,
                                            'ens_{}'.format(i+1),
                                            ens_forcing_prefix)),
                                   ('OUTFILE', 'history.ens{}'.format(i+1))])
            global_file = generate_VIC_global_file(
                                global_template_path=vic_global_template_file,
                                model_steps_per_day=vic_model_steps_per_day,
                                start_time=start_time,
                                end_time=end_time,
                                init_state="INIT_STATE {}".format(
                                                os.path.join(init_state_dir,
                                                             'state.ens{}.nc'.format(i+1))),
                                vic_state_basepath=os.path.join(out_state_dir,
                                                                'state.ens{}'.format(i+1)),
                                vic_history_file_dir=out_history_dir,
                                replace=replace,
                                output_global_basepath=os.path.join(
                                            out_global_dir,
                                            'global.ens{}'.format(i+1)))
            # Run VIC
            pool.apply_async(run_vic_for_multiprocess,
                             (vic_exe, global_file, out_log_dir, mpi_proc, mpi_exe))
        
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()


def determine_tile_frac(global_path):
    ''' Determines the fraction of each veg/snowband tile in each grid cell based on VIC
        global and parameter files
    
    Parameters
    ----------
    global_path: <str>
        VIC global parameter file path; can be a template file (here it is only used to
        extract snowband and vegparam files/options)
    
    Returns
    ----------
    da_tile_frac: <xr.DataArray>
        Fraction of each veg/snowband in each grid cell for the whole domain
        Dimension: [veg_class, snow_band, lat, lon]
    
    Require
    ----------
    numpy
    xarray
    '''
    
    # --- Load global parameter file --- #
    with open(global_path, 'r') as global_file:
            global_param = global_file.read()
            
    # --- Extract Cv from vegparam file (as defined in the global file) --- #
    param_nc = find_global_param_value(global_param, 'PARAMETERS')   
    ds_param = xr.open_dataset(param_nc, decode_cf=False)
    da_Cv = ds_param['Cv']  # dim: [veg_class, lat, lon]
    lat = da_Cv['lat']
    lon = da_Cv['lon']
    
    # --- Extract snowband info from the global and param files --- #
    SNOW_BAND = find_global_param_value(global_param, 'SNOW_BAND')
    if SNOW_BAND.upper() == 'TRUE':
        n_snowband = len(ds_param['snow_band'])
    else:
        n_snowband = 1
    # Dimension of da_AreaFract: [snowband, lat, lon]
    if n_snowband == 1:  # if only one snowband
        data = np.ones([1, len(lat), len(lon)])
        da_AreaFract = xr.DataArray(data, coords=[[0], lat, lon],
                                    dims=['snow_band', 'lat', 'lon'])
    else:  # if more than one snowband
        da_AreaFract = ds_param['AreaFract']

    # --- Initialize the final DataArray --- #
    veg_class = da_Cv['veg_class']
    snow_band = da_AreaFract['snow_band']
    data = np.empty([len(veg_class), len(snow_band), len(lat), len(lon)])
    data[:] = np.nan
    da_tile_frac = xr.DataArray(data, coords=[veg_class, snow_band, lat, lon],
                                dims=['veg_class', 'snow_band', 'lat', 'lon'])
    
    # --- Calculate fraction of each veg/snowband tile for each grid cell,
    # and fill in da_file_frac --- #
    # Determine the total number of loops
    nloop = len(lat) * len(lon)
    # Convert Cv and AreaFract to np.array and straighten lat and lon into nloop
    Cv = da_Cv.values.reshape([len(veg_class), nloop])  # [nveg, nloop]
    AreaFract = da_AreaFract.values.reshape([len(snow_band), nloop])  # [nsnow, nloop]

    # Multiply Cv and AreaFract for each tile and grid cell
    tile_frac = np.array(list(map(
                    lambda i: np.dot(
                        Cv[:, i].reshape([len(veg_class), 1]),
                        AreaFract[:, i].reshape([1, len(snow_band)])),
                    range(nloop))))  # [nloop, nveg, nsnow]

    # Reshape tile_frac
    tile_frac = np.rollaxis(tile_frac, 0, 3)  # [nveg, nsow, nloop]
    tile_frac = tile_frac.reshape([len(veg_class), len(snow_band), len(lat), len(lon)])

    # Put in da_tile_frac
    da_tile_frac[:] = tile_frac
    
    return da_tile_frac


def get_soil_moisture_and_estimated_meas_all_ensemble(N, state_dir, state_time,
                                                      da_tile_frac, nproc):
    ''' This function extracts soil moisture states from netCDF state files for all ensemble
        members, for all grid cells, veg and snow band tiles.
    
    Parameters
    ----------
    N: <int>
        Number of ensemble members
    state_dir: <str>
        Directory of state files for each ensemble member
        State file names are "state.ens<i>.xxx.nc", where <i> is 1, 2, ..., N
    state_time: <pd.datetime>
        State time. This is for identifying state file names.
    da_tile_frac: <xr.DataArray>
        Fraction of each veg/snowband in each grid cell for the whole domain
        Dimension: [veg_class, snow_band, lat, lon]
    nproc: <int>
        Number of processors to use for parallel ensemble
        Default: 1

    Returns
    ----------
    da_x: <xr.DataArray>
        Soil moisture states of all ensemble members;
        Dimension: [lat, lon, n, N]
    da_y_est: <xr.DataArray>
        Estimated measurement of all ensemble members (= top-layer soil moisture);
        Dimension: [lat, lon, m, N]
    
    Require
    ----------
    xarray
    os
    States
    '''
    
    # --- Extract dimensions from the first ensemble member --- #
    state_name = 'state.ens1.{}_{:05d}.nc'.format(
                        state_time.strftime('%Y%m%d'),
                        state_time.hour*3600+state_time.second)
    ds = xr.open_dataset(os.path.join(state_dir, state_name))
    # nlayer
    nlayer = ds['nlayer']
    # veg_class
    veg_class = ds['veg_class']
    # snow_band
    snow_band = ds['snow_band']
    # lat
    lat = ds['lat']
    # lon
    lon = ds['lon']
    # number of total states n = len(veg_class) * len(snow_band) * len(nlayer)
    n = len(veg_class) * len(snow_band) * len(nlayer)
    
    # --- Initialize da for states and measurement estimates --- #
    # Initialize states x [lat, lon, n, N]
    data = np.empty([len(lat), len(lon), n, N])
    data[:] = np.nan
    da_x = xr.DataArray(data,
                        coords=[lat, lon, range(n), range(N)],
                        dims=['lat', 'lon', 'n', 'N'])
    # Initialize measurement estimates y_est [lat, lon, m, N]
    data = np.empty([len(lat), len(lon), 1, N])
    data[:] = np.nan
    da_y_est = xr.DataArray(data,
                        coords=[lat, lon, [1], range(N)],
                        dims=['lat', 'lon', 'm', 'N'])
    
    # --- Loop over each ensemble member --- #
    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        for i in range(N):
            # Load state file
            state_name = 'state.ens{}.{}_{:05d}.nc'.format(
                    i+1,
                    state_time.strftime('%Y%m%d'),
                    state_time.hour*3600+state_time.second)
            ds = xr.open_dataset(os.path.join(state_dir, state_name))
            class_states = States(ds)
            
            # Fill x and y_est data in
            # Fill in states x
            da_x.loc[:, :, :, i] = class_states.da_EnKF
            # Fill in measurement estimates y
            da_y_est[:, :, :, i] = calculate_y_est_whole_field(
                                            class_states.ds['STATE_SOIL_MOISTURE'],
                                            da_tile_frac)
    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        results = {}
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            # Load state file
            state_name = 'state.ens{}.{}_{:05d}.nc'.format(
                    i+1,
                    state_time.strftime('%Y%m%d'),
                    state_time.hour*3600+state_time.second)
            ds = xr.open_dataset(os.path.join(state_dir, state_name))
            class_states = States(ds)
 
            # Fill x and y_est data in
            # Fill in states x
            da_x.loc[:, :, :, i] = class_states.da_EnKF
            # Fill in measurement estimates y
            results[i] = pool.apply_async(
                            calculate_y_est_whole_field,
                            (class_states.ds['STATE_SOIL_MOISTURE'],
                             da_tile_frac))
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()
        # --- Get return values --- #
        list_da_perturbation = []
        for i, result in results.items():
            da_y_est[:, :, :, i] = result.get()

    return da_x, da_y_est


def calculate_y_est(x_cell, tile_frac_cell):
    ''' Caclulate estimated measurement y_est = Hx for one grid cell; here y_est is
        calculated as tile-average top-layer soil moisture over the whole grid cell.
    
    Parameters
    ----------
    x_cell: <np.array>
        An array of VIC soil moisture states for a grid cell
        Dimension: [veg_class, snow_band, nlayer]
    tile_frac_cell: <np.array>
        An array of veg/band tile fraction for a grid cell
        Dimension: [veg_class, snow_band]
    
    Returns
    ----------
    y_est: <np.float>
        Estimated measurement for this grid cell
    
    Require
    ----------
    numpy
    '''
    
    # Calculate y_est
    y_est = np.nansum(x_cell[:, :, 0] * tile_frac_cell)
    
    return y_est


def calculate_y_est_whole_field(da_x, da_tile_frac):
    ''' Calculate estimated measurement y_est = Hx for all grid cells.
    
    Parameters
    ----------
    da_x: <xr.DataArray>
        A DataArray of VIC soil moisture states for all grid cells
        Dimension: [veg_class, snow_band, nlayer, lat, lon]
    da_tile_frac: <xr.DataArray>
        Fraction of each veg/snowband in each grid cell for the whole domain
        Dimension: [veg_class, snow_band, lat, lon]
        
    Returns
    ----------
    da_y_est: <xr.DataArray>
        Estimated measurement (= top-layer soil moisture) for all grid cells;
        Dimension: [lat, lon, m]
        
    
    Require
    ----------
    xarray
    numpy
    '''
    
    # --- Extract coords --- #
    lat = da_x['lat']
    lon = da_x['lon']
    veg_class = da_x['veg_class']
    snow_band = da_x['snow_band']
    nlayer = da_x['nlayer']
    
    # --- Initiate da_y_est --- #
    data = np.empty([len(lat), len(lon), 1])
    da_y_est = xr.DataArray(data, coords=[lat, lon, [0]], dims=['lat', 'lon', 'm'])
    
    # --- Calculate y_est for all grid cells --- #
    # Determine the total number of loops
    nloop = len(lat) * len(lon)
    # Convert da_x and da_tile_frac to np.array and straighten lat and lon into nloop
    x = da_x.values.reshape([len(veg_class), len(snow_band),
                            len(nlayer), nloop])  # [nveg, nsnow, nlayer, nloop]
    tile_frac = da_tile_frac.values.reshape([len(veg_class), len(snow_band),
                                             nloop])  # [nveg, nsnow, nloop]
    # Calculate y_est for all grid cells
    y_est = np.array(list(map(
                lambda i: calculate_y_est(x[:, :, :, i],
                                          tile_frac[:, :, i]),
                range(nloop)))).reshape([nloop, 1])  # [nloop, m=1]
    # Reshape y_est
    y_est = y_est.reshape([len(lat), len(lon), 1])  # [lat, lon, m=1]
    # Put in da_y_est
    da_y_est[:] = y_est
    
    return da_y_est


def find_global_param_value(gp, param_name, second_param=False):
    ''' Return the value of a global parameter

    Parameters
    ----------
    gp: <str>
        Global parameter file, read in by read()
    param_name: <str>
        The name of the global parameter to find
    second_param: <bool>
        Whether to read a second value for the parameter (e.g., set second_param=True to
        get the snowband param file path when SNOW_BAND>1)

    Returns
    ----------
    line_list[1]: <str>
        The value of the global parameter
    (optional) line_list[2]: <str>
        The value of the second value in the global parameter file when second_param=True
    '''
    for line in iter(gp.splitlines()):
        line_list = line.split()
        if line_list == []:
            continue
        key = line_list[0]
        if key == param_name:
            if second_param == False:
                return line_list[1]
            else:
                return line_list[1], line_list[2]


def calculate_gain_K(x, y_est, R):
    ''' This function calculates Kalman gain K from ensemble.
    
    Parameters
    ----------
    x: <np.array> [n*N]
        An array of forecasted ensemble states (before updated)
    y_est: <np.array> [m*N]
        An array of forecasted ensemble measurement estimates (before updated);
        (y_est = Hx)
    R: <np.array> [m*m]
        Measurement error covariance matrix
    
    Returns
    ----------
    K: <np.array> [n*m]
        Gain K
    
    Require
    ----------
    numpy
    '''
    
    # Extract number of EnKF states (n) and number of measurements (m)
    n = np.shape(x)[0]
    m = np.shape(y_est)[0]
    
    # Pxy = cov(x, y.transpose); size = [n*m]; divided by (N-1)
    Pxy = np.cov(x, y_est)[:n, n:]
    # Pyy = cov(y, y.transpose); size = [m*m]; divided by (N-1)
    Pyy = np.cov(y_est)
    # K = Pxy * (Pyy)-1
    if m == 1:  # if m = 1
        K = Pxy / (Pyy + R)
    else:  # if m > 1
        K = np.dot(Pxx, np.linalg.inv(Pyy+R))

    return K


def calculate_gain_K_whole_field(da_x, da_y_est, R):
    ''' This function calculates gain K over the whole field.
    
    Parameters
    ----------
    da_x: <xr.DataArray>
        Soil moisture states of all ensemble members;
        As returned from get_soil_moisture_and_estimated_meas_all_ensemble;
        Dimension: [lat, lon, n, N]
    da_y_est: <xr.DataArray>
        Estimated measurement of all ensemble members;
        As returned from get_soil_moisture_and_estimated_meas_all_ensemble;
        Dimension: [lat, lon, m, N]
    R: <np.array> [m*m]
        Measurement error covariance matrix
        
    Returns
    ----------
    da_K: <xr.DataArray>
        Gain K for the whole field
        Dimension: [lat, lon, n, m], where [n, m] is the Kalman gain K
    
    Require
    ----------
    xarray
    calculate_gain_K
    numpy
    '''

    # --- Extract dimensions --- #
    lat_coord = da_x['lat']
    lon_coord = da_x['lon']
    n_coord = da_x['n']
    N_coord = da_x['N']
    m_coord = da_y_est['m']
    
    # --- Initialize da_K --- #
    K = np.empty([len(lat_coord), len(lon_coord), len(n_coord), len(m_coord)])
    K[:] = np.nan
    da_K = xr.DataArray(K,
                        coords=[lat_coord, lon_coord, n_coord, m_coord],
                        dims=['lat', 'lon', 'n', 'm'])
    
    # --- Calculate gain K for the whole field --- #
    # Determine the total number of loops
    nloop = len(lat_coord) * len(lon_coord)
    # Convert da_x and da_y_est to np.array and straighten lat and lon into nloop
    x = da_x.values.reshape([nloop, len(n_coord), len(N_coord)])  # [nloop, n, N]
    y_est = da_y_est.values.reshape([nloop, len(m_coord), len(N_coord)])  # [nloop, m, N]
    # Calculate gain K for the whole field
    K = np.array(list(map(
                lambda i: calculate_gain_K(x[i, :, :], y_est[i, :, :], R),
                range(nloop))))  # [nloop, n, m]
    # Reshape K
    K = K.reshape([len(lat_coord), len(lon_coord), len(n_coord),
                   len(m_coord)])  # [lat, lon, n, m]
    # Put in da_K
    da_K[:] = K

    return da_K


def update_states(da_y_est, da_K, da_meas, R, state_nc_before_update,
                  out_vic_state_nc, da_max_moist_n,
                  adjust_negative=True, seed=None):
    ''' Update the EnKF states for the whole field.
    
    Parameters
    ----------
    da_y_est: <xr.DataArray>
        Estimated measurement from pre-updated states (y = Hx);
        Dimension: [lat, lon, m]
    da_K: <xr.DataArray>
        Gain K for the whole field
        Dimension: [lat, lon, n, m], where [n, m] is the Kalman gain K
    da_meas: <xr.DataArray> [lat, lon, m]
        Measurements at current time
    R: <float> (for m = 1)
        Measurement error covariance matrix (measurement error ~ N(0, R))
    state_nc_before_update: <str>
        VIC state file before update
    out_vic_state_nc: <str>
        Path for saving updated state file in VIC format;
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    adjust_negative: <bool>
        Whether or not to adjust negative soil moistures after update
        to zero.
        Default: True (adjust negative to zero)
    seed: <int or None>
        Seed for random number generator; this seed will only be used locally
        in this function and will not affect the upper-level code.
        None for not re-assign seed in this function, but using the global seed)
        Default: None
    
    Returns
    ----------
    da_x_updated: <xr.DataArray>
        Updated soil moisture states;
        Dimension: [lat, lon, n]
    da_update_increm: <xr.DataArray>
        Update increment of soil moisture states
        Dimension: [lat, lon, n]
    da_v: <xr.DataArray>
        Measurement perturbation in the update step
        Dimension: [lat, lon, m]
    
    Require
    ----------
    numpy
    '''

    # Load VIC states before update for this ensemble member          
    ds = xr.open_dataset(state_nc_before_update)
    # Convert EnKF states back to VIC states
    class_states = States(ds)
    # Update states
    da_x_updated, da_v = class_states.update_soil_moisture_states(
                                        da_K, da_meas,
                                        da_y_est, R,
                                        da_max_moist_n,
                                        adjust_negative, seed)  # [lat, lon, n]
    da_update_increm = da_x_updated - class_states.da_EnKF
    # Convert updated states to VIC states format
    ds_updated = class_states.convert_new_EnKFstates_sm_to_VICstates(da_x_updated)
    # Save VIC states to netCDF file (with compression)
    to_netcdf_state_file_compress(ds_updated, out_vic_state_nc)

    return da_x_updated, da_update_increm, da_v


def update_states_ensemble(da_y_est, da_K, da_meas, R, state_dir_before_update,
                           state_time, out_vic_state_dir, da_max_moist_n,
                           adjust_negative=True, nproc=1):
    ''' Update the EnKF states for the whole field for each ensemble member.
    
    Parameters
    ----------
    da_y_est: <xr.DataArray>
        Estimated measurement from pre-updated states of all ensemble members (y = Hx);
        Dimension: [lat, lon, m, N]
    da_K: <xr.DataArray>
        Gain K for the whole field
        Dimension: [lat, lon, n, m], where [n, m] is the Kalman gain K
    da_meas: <xr.DataArray> [lat, lon, m]
        Measurements at current time
    R: <float> (for m = 1)
        Measurement error covariance matrix (measurement error ~ N(0, R))
    state_dir_before_update: <str>
        Directory of VIC states before update;
        State file names are: state.ens<i>.<YYYYMMDD>_<SSSSS>.nc,
        where <i> is ensemble member index (1, ..., N),
              <YYYYMMMDD>_<SSSSS> is the current time of the states
    state_time: <pd.datetime>
        State time. This is for identifying state file names.
    output_vic_state_dir: <str>
        Directory for saving updated state files in VIC format;
        State file names will be: state.ens<i>.nc, where <i> is ensemble member index (1, ..., N)
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    adjust_negative: <bool>
        Whether or not to adjust negative soil moistures after update
        to zero.
        Default: True (adjust negative to zero)
    nproc: <int>
        Number of processors to use for parallel ensemble
        Default: 1
    
    Returns
    ----------
    da_x_updated: <xr.DataArray>
        Updated soil moisture states;
        Dimension: [N, lat, lon, n]
    da_update_increm: <xr.DataArray>
        Update increment of soil moisture states
        Dimension: [N, lat, lon, n]
    da_v: <xr.DataArray>
        Measurement perturbation in the update step
        Dimension: [N, lat, lon, m]
    
    Require
    ----------
    numpy
    '''

    # Extract N
    N = len(da_y_est['N'])

    list_da_update_increm = []  # update increment
    list_da_v = []  # measurement perturbation in the update step
    list_da_x_updated = []  # measurement perturbation in the update step

    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        for i in range(N):
            # Set up parameters
            state_nc_before_update = os.path.join(
                    state_dir_before_update,
                    'state.ens{}.{}_{:05d}.nc'.format(
                            i+1,
                            state_time.strftime('%Y%m%d'),
                            state_time.hour*3600+state_time.second))
            out_vic_state_nc = os.path.join(out_vic_state_dir,
                                            'state.ens{}.nc'.format(i+1))
            # Update states
            seed = np.random.randint(low=100000)
            da_x_updated, da_update_increm, da_v = update_states(
                        da_y_est.loc[:, :, :, i], da_K, da_meas, R,
                        state_nc_before_update,
                        out_vic_state_nc, da_max_moist_n,
                        adjust_negative, seed)
            # Put results to list
            list_da_v.append(da_v)
            list_da_x_updated.append(da_x_updated)
            list_da_update_increm.append(da_update_increm)
    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        results = {}
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            # Set up parameters
            state_nc_before_update = os.path.join(
                    state_dir_before_update,
                    'state.ens{}.{}_{:05d}.nc'.format(
                            i+1,
                            state_time.strftime('%Y%m%d'),
                            state_time.hour*3600+state_time.second))
            out_vic_state_nc = os.path.join(out_vic_state_dir,
                                            'state.ens{}.nc'.format(i+1))
            # Update states
            seed = np.random.randint(low=100000)
            results[i] = pool.apply_async(
                            update_states,
                            (da_y_est.loc[:, :, :, i], da_K, da_meas, R,
                            state_nc_before_update,
                            out_vic_state_nc, da_max_moist_n,
                            adjust_negative, seed))
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()
        # --- Get return values --- #
        for i, result in results.items():
            da_x_updated, da_update_increm, da_v = result.get()
            # Put results to list
            list_da_v.append(da_v)
            list_da_x_updated.append(da_x_updated)
            list_da_update_increm.append(da_update_increm)

    # --- Put update increment and measurement perturbation of all ensemble
    # members into one da --- #
    da_x_updated = xr.concat(list_da_x_updated, dim='N')
    da_x_updated['N'] = range(1, N+1)
    da_update_increm = xr.concat(list_da_update_increm, dim='N')
    da_update_increm['N'] = range(1, N+1)
    da_v = xr.concat(list_da_v, dim='N')
    da_v['N'] = range(1, N+1)
    
    return da_x_updated, da_update_increm, da_v


def perturb_forcings_ensemble(N, orig_forcing, dict_varnames, prec_std,
                              prec_phi, out_forcing_basedir):
    ''' Perturb forcings for all ensemble members

    Parameters
    ----------
    N: <int>
        Number of ensemble members
    orig_forcing: <class 'Forcings'>
        Original (unperturbed) VIC forcings
    dict_varnames: <dict>
        A dictionary of forcing names in nc file;
        e.g., {'PREC': 'prcp'; 'AIR_TEMP': 'tas'}
    prec_std: <float>
        Standard deviation of the precipitation perturbing multiplier
    prec_phi: <float>
        Parameter in AR(1) process for precipitation noise.
    out_forcing_basedir: <str>
        Base directory for output perturbed forcings;
        Subdirs "ens_<i>" will be created, where <i> is ensemble index, 1, ..., N
        File names will be: forc.YYYY.nc 

    Require
    ----------
    os
    '''
    
    # Loop over each ensemble member
    for i in range(N):
        # Setup subdir
        subdir = setup_output_dirs(
                    out_forcing_basedir,
                    mkdirs=['ens_{}'.format(i+1)])['ens_{}'.format(i+1)]

        # Perturb PREC
        ds_perturbed = orig_forcing.perturb_prec_lognormal(
                                            varname=dict_varnames['PREC'],
                                            std=prec_std,
                                            phi=prec_phi)
        # Save to nc file
        for year, ds in ds_perturbed.groupby('time.year'):
            to_netcdf_forcing_file_compress(
                    ds, os.path.join(subdir,
                    'force.{}.nc'.format(year)))


def replace_global_values(gp, replace):
    '''given a multiline string that represents a VIC global parameter file,
       loop through the string, replacing values with those found in the
       replace dictionary'''
    
    gpl = []
    for line in iter(gp.splitlines()):
        line_list = line.split()
        if line_list:
            key = line_list[0]
            if key in replace:
                value = replace.pop(key)
                val = list([str(value)])
            else:
                val = line_list[1:]
            gpl.append('{0: <20} {1}\n'.format(key, ' '.join(val)))

    if replace:
        for key, val in replace.items():
            try:
                value = ' '.join(val)
            except:
                value = val
            gpl.append('{0: <20} {1}\n'.format(key, value))

    return gpl


def perturb_soil_moisture_states_ensemble(N, states_to_perturb_dir,
                                          L, scale_n_nloop,
                                          out_states_dir,
                                          da_max_moist_n,
                                          adjust_negative=True,
                                          nproc=1):
    ''' Perturb all soil_moisture states for each ensemble member
    
    Parameters
    ----------
    N: <int>
        Number of ensemble members
    states_to_perturb_dir: <str>
        Directory for VIC state files to perturb.
        File names: state.ens<i>.nc, where <i> is ensemble index 1, ..., N
    L: <np.array>
        Cholesky decomposed matrix of covariance matrix P of all states:
                        P = L * L.T
        Thus, L * Z (where Z is i.i.d. standard normal random variables of
        dimension [n, n]) is multivariate normal random variables with
        mean zero and covariance matrix P.
        Dimension: [n, n]
    scale_n_nloop: <np.array>
        Standard deviation of noise to add for the whole field.
        Dimension: [nloop, n] (where nloop = lat * lon)
    out_states_dir: <str>
        Directory for output perturbed VIC state files;
        File names: state.ens<i>.nc, where <i> is ensemble index 1, ..., N
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    adjust_negative: <bool>
        Whether or not to adjust negative soil moistures after
        perturbation to zero.
        Default: True (adjust negative to zero)
    nproc: <int>
        Number of processors to use for parallel ensemble
        Default: 1

    Return
    ----------
    list_da_perturbation: <list>
        A list of amount of perturbation added (a list of each ensemble member)
        Dimension of each da: [veg_class, snow_band, nlayer, lat, lon]
    
    Require
    ----------
    os
    class States
    '''

    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        list_da_perturbation = [] 
        for i in range(N):
            states_to_perturb_nc = os.path.join(
                    states_to_perturb_dir,
                    'state.ens{}.nc'.format(i+1))
            out_states_nc = os.path.join(out_states_dir,
                                         'state.ens{}.nc'.format(i+1))
            seed = np.random.randint(low=100000)
            da_perturbation = perturb_soil_moisture_states(
                                         states_to_perturb_nc, L, scale_n_nloop,
                                         out_states_nc, da_max_moist_n,
                                         adjust_negative, seed)
            list_da_perturbation.append(da_perturbation)
    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        results = {}
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            states_to_perturb_nc = os.path.join(
                    states_to_perturb_dir,
                    'state.ens{}.nc'.format(i+1))
            out_states_nc = os.path.join(out_states_dir,
                                         'state.ens{}.nc'.format(i+1))
            seed = np.random.randint(low=100000)
            results[i] = pool.apply_async(
                    perturb_soil_moisture_states,
                    (states_to_perturb_nc, L, scale_n_nloop,
                     out_states_nc, da_max_moist_n,
                     adjust_negative, seed))
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()

        # --- Get return values --- #
        list_da_perturbation = [] 
        for i, result in results.items():
            list_da_perturbation.append(result.get())

    return list_da_perturbation


def propagate(start_time, end_time, vic_exe, vic_global_template_file,
                       vic_model_steps_per_day, init_state_nc, out_state_basepath,
                       out_history_dir, out_history_fileprefix,
                       out_global_basepath, out_log_dir,
                       forcing_basepath, mpi_proc=None, mpi_exe='mpiexec'):
    ''' This function propagates (via VIC) from an initial state (or no initial state)
        to a certain time point.

    Parameters
    ----------
    start_time: <pandas.tslib.Timestamp>
        Start time of this propagation run
    end_time: <pandas.tslib.Timestamp>
        End time of this propagation
    vic_exe: <class 'VIC'>
        Tonic VIC class
    vic_global_template_file: <str>
        Path of VIC global file template
    vic_model_steps_per_day: <str>
        VIC option - model steps per day
    init_state_nc: <str>
        Initial state netCDF file; None for no initial state
    out_state_basepath: <str>
        Basepath of output states; ".YYYYMMDD_SSSSS.nc" will be appended.
        None if do not want to output state file
    out_history_dir: <str>
        Directory of output history files
    out_history_fileprefix: <str>
        History file prefix
    out_global_basepath: <str>
        Basepath of output global files; "YYYYMMDD-HHS_YYYYMMDD.txt" will be appended
    out_log_dir: <str>
        Directory for output log files
    forcing_basepath: <str>
        Forcing basepath. <YYYY.nc> will be appended
    mpi_proc: <int or None>
        Number of processors to use for VIC MPI run. None for not using MPI
        Default: None
    mpi_exe: <str>
        Path for MPI exe. Only used if mpi_proc is not None


    Require
    ----------
    OrderedDict
    generate_VIC_global_file
    check_returncode
    '''

    # Generate VIC global param file
    replace = OrderedDict([('FORCING1', forcing_basepath),
                           ('OUTFILE', out_history_fileprefix)])
    global_file = generate_VIC_global_file(
                        global_template_path=vic_global_template_file,
                        model_steps_per_day=vic_model_steps_per_day,
                        start_time=start_time,
                        end_time=end_time,
                        init_state='#INIT_STATE' if init_state_nc is None
                                   else 'INIT_STATE {}'.format(init_state_nc),
                        vic_state_basepath=out_state_basepath,
                        vic_history_file_dir=out_history_dir,
                        replace=replace,
                        output_global_basepath=out_global_basepath)
    
    # Run VIC
    returncode = vic_exe.run(global_file, logdir=out_log_dir,
                             **{'mpi_proc': mpi_proc, 'mpi_exe': mpi_exe})
    check_returncode(returncode, expected=0)

    # Delete log files (to save space)
    for f in glob.glob(os.path.join(out_log_dir, "*")):
        os.remove(f)


def propagate_linear_model(start_time, end_time, lat_coord, lon_coord,
                       model_steps_per_day, init_state_nc, out_state_basepath,
                       out_history_dir, out_history_fileprefix,
                       forcing_basepath, prec_varname, dict_linear_model_param):
    ''' This function propagates (via VIC) from an initial state (or no
        initial state) to a certain time point for the whole domain.
        The linear model assumes 3 soil layers and only allows 1 tile per
        grid cell (i.e., nveg=1 and nsnow=1). Specifically, for each grid cell,
        the model takes the following form:
                    sm(t+1) = r * sm(t) + P(t)
        where sm(t) is an array of dim [3, 1], r is a constant matrix of
        dim [3, 3], and P(t) is input precipitation and is an array of dim
        [3, 1].

    Parameters
    ----------
    start_time: <pandas.tslib.Timestamp>
        Start time of this propagation run
    end_time: <pandas.tslib.Timestamp>
        End time of this propagation
    lat_coord: <list/xr.coord>
        Latitude coordinates
    lon_coord: <list/xr.coord>
        Longitude coordinates
    model_steps_per_day: <str>
        VIC option - model steps per day
    init_state_nc: <str>
        Initial state netCDF file; None for no initial state
    out_state_basepath: <str>
        Basepath of output states; ".YYYYMMDD_SSSSS.nc" will be appended
    out_history_dir: <str>
        Directory of output history files
    out_history_fileprefix: <str>
        History file prefix
    forcing_basepath: <str>
        Forcing basepath. <YYYY.nc> will be appended
    prec_varname: <str>
        Precip varname in the forcing netCDF files
    dict_linear_model_param: <dict>
        A dict of linear model parameters.
        Keys: 'r1', 'r2', 'r3', 'r12', 'r23'
    '''
    
    # --- Establish linear propagation matrix r[3, 3] --- #
    r1 = dict_linear_model_param['r1']
    r2 = dict_linear_model_param['r2']
    r3 = dict_linear_model_param['r3']
    r12 = dict_linear_model_param['r12']
    r23 = dict_linear_model_param['r23']
    r = np.array([[r1-r12, 0, 0],
                  [r12, r2-r23, 0],
                  [0, r23, r3]])

    # --- Establish a list of time steps --- #
    dt_hour = int(24 / model_steps_per_day)  # delta t in hour
    times = pd.date_range(start_time, end_time, freq='{}H'.format(dt_hour))

    # --- Set initial states [3, lat, lon] --- #
    # If not initial states, set all initial soil moistures to zero
    if init_state_nc is None:
        sm0 = np.zeros([3, len(lat_coord), len(lon_coord)])
    # Otherwise, read from an initial state netCDF file
    else:
        sm0 = xr.open_dataset(init_state_nc)['STATE_SOIL_MOISTURE']\
              [0, 0, :, :, :].values

    # --- Load forcing data --- #
    start_year = start_time.year
    end_year = end_time.year
    list_ds = []
    for year in range(start_year, end_year+1):
        ds = xr.open_dataset(forcing_basepath + str(year) + '.nc')
        list_ds.append(ds)
    ds_force = xr.concat(list_ds, dim='time')
    
    # --- Run the linear model --- #
    sm = np.empty([len(times), 3, len(lat_coord), len(lon_coord)])  # [time, 3, lat, lon]
    for i, t in enumerate(times):
        # Extract prec forcing
        da_prec = ds_force[prec_varname].sel(time=t)  # [lat, lon]
        # Determine the total number of loops
        nloop = len(lat_coord) * len(lon_coord)
        # Convert sm into nloop
        if i == 0:
            sm_init = sm0.reshape([3, nloop])  # [3, nloop]
        else:
            sm_init = sm[i-1, :, :, :].reshape([3, nloop])
        # Convert prec into nloop
        prec = da_prec.values.reshape([nloop])
        prec2 = np.zeros(nloop)
        prec3 = np.zeros(nloop)
        prec = np.array([prec, prec2, prec3])  # [3, nloop]
        # Use linear model operate to calculate sm(t)
        sm_new = np.array(list(map(
                    lambda j: np.dot(r, sm_init[:, j]) + prec[:, j],
                    range(nloop))))  # [nloop, 3]
        # Reshape and roll lat and lon to last # [3, lat, lon]
        sm_new = sm_new.reshape([len(lat_coord), len(lon_coord), 3])
        sm_new = np.rollaxis(sm_new, 0, 3)
        sm_new = np.rollaxis(sm_new, 0, 3)
        sm_new = sm_new.reshape([3, len(lat_coord), len(lon_coord)])
        # Put sm_new into the final sm matrix
        sm[i, :, :, :] = sm_new
    
    # --- Put simulated sm into history da and save to history file --- #
    da_sm = xr.DataArray(sm,
                         coords=[times, [0, 1, 2], lat_coord, lon_coord],
                         dims=['time', 'nlayer', 'lat', 'lon'])
    ds_hist = xr.Dataset({'OUT_SOIL_MOIST': da_sm})
    ds_hist.to_netcdf(os.path.join(
            out_history_dir,
            out_history_fileprefix + \
            '.{}-{:05d}.nc'.format(start_time.strftime('%Y-%m-%d'),
                               start_time.hour*3600+start_time.second)))
    
    # --- Save state file at the end of the last time step --- #
    state_time = end_time + pd.DateOffset(hours=dt_hour)
    sm_state = sm[-1, :, :, :].reshape([1, 1, 3, len(lat_coord),
                                        len(lon_coord)])  # [1, 1, 3, lat, lon]
    da_sm_state = xr.DataArray(
            sm_state,
            coords=[[1], [0], [0, 1, 2], lat_coord, lon_coord],
            dims=['veg_class', 'snow_band', 'nlayer', 'lat', 'lon'])
    ds_state = xr.Dataset({'STATE_SOIL_MOISTURE': da_sm_state})
    ds_state.to_netcdf(
            out_state_basepath + \
            '.{}_{:05d}.nc'.format(state_time.strftime('%Y%m%d'),
                                  state_time.hour*3600+state_time.second))


def propagate_ensemble_linear_model(N, start_time, end_time, lat_coord,
                                   lon_coord, model_steps_per_day,
                                   init_state_dir, out_state_dir,
                                   out_history_dir,
                                   ens_forcing_basedir, ens_forcing_prefix,
                                   prec_varname, dict_linear_model_param,
                                   nproc=1):
    ''' This function propagates (via VIC) an ensemble of states to a certain time point.
    
    Parameters
    ----------
    N: <int>
        Number of ensemble members
    start_time: <pandas.tslib.Timestamp>
        Start time of this propagation run
    end_time: <pandas.tslib.Timestamp>
        End time of this propagation
    lat_coord: <list/xr.coord>
        Latitude coordinates
    lon_coord: <list/xr.coord>
        Longitude coordinates
    model_steps_per_day: <str>
        VIC option - model steps per day
    init_state_dir: <str>
        Directory of initial states for each ensemble member
        State file names are "state.ens<i>", where <i> is 1, 2, ..., N
    out_state_dir: <str>
        Directory of output states for each ensemble member
        State file names will be "state.ens<i>.xxx.nc", where <i> is 1, 2, ..., N
    out_history_dir: <str>
        Directory of output history files for each ensemble member
        History file names will be "history.ens<i>.nc", where <i> is 1, 2, ..., N
    ens_forcing_basedir: <str>
        Ensemble forcing basedir ('ens_{}' subdirs should be under basedir)
    ens_forcing_prefix: <str>
        Prefix of ensemble forcing filenames under 'ens_{}' subdirs
        'YYYY.nc' will be appended
    prec_varname: <str>
        Precip varname in the forcing netCDF files
    dict_linear_model_param: <dict>
        A dict of linear model parameters.
        Keys: 'r1', 'r2', 'r3', 'r12', 'r23'
    nproc: <int>
        Number of processors to use for parallel ensemble
        Default: 1
        
    Require
    ----------
    OrderedDict
    multiprocessing
    generate_VIC_global_file
    '''

    # --- If nproc == 1, do a regular ensemble loop --- #
    if nproc == 1:
        for i in range(N):
            # Prepare linear model parameters
            init_state_nc = os.path.join(init_state_dir,
                                         'state.ens{}.nc'.format(i+1))
            out_state_basepath = os.path.join(out_state_dir,
                                              'state.ens{}'.format(i+1))
            out_history_fileprefix = 'history.ens{}'.format(i+1)
            forcing_basepath = os.path.join(
                    ens_forcing_basedir,
                    'ens_{}'.format(i+1), ens_forcing_prefix)
            # Run linear model
            propagate_linear_model(
                    start_time, end_time, lat_coord, lon_coord,
                    model_steps_per_day, init_state_nc, out_state_basepath,
                    out_history_dir, out_history_fileprefix,
                    forcing_basepath, prec_varname, dict_linear_model_param)

    # --- If nproc > 1, use multiprocessing --- #
    elif nproc > 1:
        # --- Set up multiprocessing --- #
        pool = mp.Pool(processes=nproc)
        # --- Loop over each ensemble member --- #
        for i in range(N):
            # Prepare linear model parameters
            init_state_nc = os.path.join(init_state_dir,
                                         'state.ens{}.nc'.format(i+1))
            out_state_basepath = os.path.join(out_state_dir,
                                              'state.ens{}'.format(i+1))
            out_history_fileprefix = 'history.ens{}'.format(i+1)
            forcing_basepath = os.path.join(
                    ens_forcing_basedir,
                    'ens_{}'.format(i+1), ens_forcing_prefix)
            # Run linear model
            pool.apply_async(propagate_linear_model,
                             (start_time, end_time, lat_coord, lon_coord,
                              model_steps_per_day, init_state_nc,
                              out_state_basepath, out_history_dir,
                              out_history_fileprefix, forcing_basepath,
                              prec_varname, dict_linear_model_param))
        # --- Finish multiprocessing --- #
        pool.close()
        pool.join()


def perturb_soil_moisture_states(states_to_perturb_nc, L, scale_n_nloop,
                                 out_states_nc, da_max_moist_n,
                                 adjust_negative=True, seed=None):
    ''' Perturb all soil_moisture states

    Parameters
    ----------
    states_to_perturb_nc: <str>
        Path of VIC state netCDF file to perturb
    L: <np.array>
        Cholesky decomposed matrix of covariance matrix P of all states:
                        P = L * L.T
        Thus, L * Z (where Z is i.i.d. standard normal random variables of
        dimension [n, n]) is multivariate normal random variables with
        mean zero and covariance matrix P.
        Dimension: [n, n]
    scale_n_nloop: <np.array>
        Standard deviation of noise to add for the whole field.
        Dimension: [nloop, n] (where nloop = lat * lon)
    out_states_nc: <str>
        Path of output perturbed VIC state netCDF file
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    adjust_negative: <bool>
        Whether or not to adjust negative soil moistures after
        perturbation to zero.
        Default: True (adjust negative to zero)
    seed: <int or None>
        Seed for random number generator; this seed will only be used locally
        in this function and will not affect the upper-level code.
        None for not re-assign seed in this function, but using the global seed)
        Default: None

    Returns
    ----------
    da_perturbation: <da.DataArray>
        Amount of perturbation added
        Dimension: [veg_class, snow_band, nlayer, lat, lon]

    Require
    ----------
    os
    class States
    '''

    # --- Load in original state file --- #
    class_states = States(xr.open_dataset(states_to_perturb_nc))

    # --- Perturb --- #
    # Perturb
    ds_perturbed = class_states.perturb_soil_moisture_Gaussian(
                            L, scale_n_nloop, da_max_moist_n,
                            adjust_negative, seed)

    # --- Save perturbed state file --- #
    ds_perturbed.to_netcdf(out_states_nc,
                           format='NETCDF4_CLASSIC')

    # --- Return perturbation --- #
    da_perturbation = (ds_perturbed - class_states.ds)['STATE_SOIL_MOISTURE']

    return da_perturbation


def perturb_soil_moisture_states_class_input(class_states, L, scale_n_nloop,
                                 out_states_nc, da_max_moist_n,
                                 adjust_negative=True, seed=None):
    ''' Perturb all soil_moisture states (same as funciton
        perturb_soil_moisture_states except here inputing class_states
        instead of an netCDF path for states to perturb)

    Parameters
    ----------
    class_states: <class States>
        VIC state netCDF file to perturb
    L: <np.array>
        Cholesky decomposed matrix of covariance matrix P of all states:
                        P = L * L.T
        Thus, L * Z (where Z is i.i.d. standard normal random variables of
        dimension [n, n]) is multivariate normal random variables with
        mean zero and covariance matrix P.
        Dimension: [n, n]
    scale_n_nloop: <np.array>
        Standard deviation of noise to add for the whole field.
        Dimension: [nloop, n] (where nloop = lat * lon)
    out_states_nc: <str>
        Path of output perturbed VIC state netCDF file
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile
        [unit: mm]. Soil moistures above maximum after perturbation will
        be reset to maximum value.
        Dimension: [lat, lon, n]
    adjust_negative: <bool>
        Whether or not to adjust negative soil moistures after
        perturbation to zero.
        Default: True (adjust negative to zero)
    seed: <int or None>
        Seed for random number generator; this seed will only be used locally
        in this function and will not affect the upper-level code.
        None for not re-assign seed in this function, but using the global seed)
        Default: None

    Returns
    ----------
    da_perturbation: <da.DataArray>
        Amount of perturbation added
        Dimension: [veg_class, snow_band, nlayer, lat, lon]

    Require
    ----------
    os
    class States
    '''

    # --- Perturb --- #
    # Perturb
    ds_perturbed = class_states.perturb_soil_moisture_Gaussian(
                            L, scale_n_nloop, da_max_moist_n,
                            adjust_negative, seed)

    # --- Save perturbed state file --- #
    to_netcdf_state_file_compress(ds_perturbed, out_states_nc)

    # --- Return perturbation --- #
    da_perturbation = (ds_perturbed - class_states.ds)['STATE_SOIL_MOISTURE']

    return da_perturbation


def concat_vic_history_files(list_history_nc):
    ''' Concatenate short-periods of VIC history files into one; if the time period of
        the next file overlaps that of the current file, the next-file values will be
        used
    
    list_history_nc: <list>
        A list of history files to be concatenated, in order
    '''
    
    print('\tConcatenating {} files...'.format(len(list_history_nc)))

    # --- Open all history files --- #
    list_ds = []
    for file in list_history_nc:
        list_ds.append(xr.open_dataset(file))

    # --- Loop over each history file and concatenate --- #
    list_ds_to_concat = []  # list of ds to concat, with no overlapping periods
    for i in range(len(list_ds[:-1])):
        # Determine and truncate data, if needed
        times_current = pd.to_datetime(list_ds[i]['time'].values)  # times of current ds
        times_next = pd.to_datetime(list_ds[i+1]['time'].values)  # times of next ds
        if times_current[-1] >= times_next[0]:  # if overlap, truncate the current ds
            # Minus 2 seconds to avoid resolution issue
            trunc_time_point = times_next[0] - pd.DateOffset(seconds=2) 
            ds = list_ds[i].sel(time=slice(None, '{}T{:02d}:{:02d}:{:02d}'.format(
                                                trunc_time_point.strftime('%Y-%m-%d'),
                                                trunc_time_point.hour,
                                                trunc_time_point.minute,
                                                trunc_time_point.second)))
        else:  # if no overlap, do not truncate
            ds = list_ds[i]
        # Concat to the list
        list_ds_to_concat.append(ds)
        
    # Concat the last period fully to the list
    list_ds_to_concat.append(list_ds[-1])
   
    # Concat all ds's
    ds_concat = xr.concat(list_ds_to_concat, dim='time')
    
    return ds_concat


def calculate_max_soil_moist_domain(global_path):
    ''' Calculates maximum soil moisture for all grid cells and all soil layers (from soil parameters)
    
    Parameters
    ----------
    global_path: <str>
        VIC global parameter file path; can be a template file (here it is only used to
        extract soil parameter file info)
        
    Returns
    ----------
    da_max_moist: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each soil layer [unit: mm];
        Dimension: [nlayer, lat, lon]
    
    Require
    ----------
    xarray
    find_global_param_value
    '''
    
     # Load soil parameter file (as defined in global file)
    with open(global_path, 'r') as global_file:
        global_param = global_file.read()
    soil_nc = find_global_param_value(global_param, 'PARAMETERS')
    ds_soil = xr.open_dataset(soil_nc, decode_cf=False)
    
    # Calculate maximum soil moisture for each layer
    # Dimension: [nlayer, lat, lon]
    da_depth = ds_soil['depth']  # [m]
    da_bulk_density = ds_soil['bulk_density']  # [kg/m3]
    da_soil_density = ds_soil['soil_density']  # [kg/m3]
    da_porosity = 1 - da_bulk_density / da_soil_density
    da_max_moist = da_depth * da_porosity * 1000  # [mm]

    return da_max_moist


def convert_max_moist_n_state(da_max_moist, nveg, nsnow):
    ''' Converts da_max_moist of dimension [nlayer, lat, lon] to [lat, lon, n]
        (Max moistures for each tile within a certain grid cell and layer are the same)

    Parameters
    ----------
    da_max_moist: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each soil layer [unit: mm];
        Dimension: [nlayer, lat, lon]
    nveg: <int>
        Number of veg classes
    nsnow: <int>
        Number of snow bands

    Returns
    ----------
    da_max_moist_n: <xarray.DataArray>
        Maximum soil moisture for the whole domain and each tile [unit: mm];
        Dimension: [lat, lon, n]
    '''
 
    # Extract coordinates
    nlayer = da_max_moist['nlayer']
    lat = da_max_moist['lat']
    lon = da_max_moist['lon']
    n = len(nlayer) * nveg * nsnow

    # Roll dimension to [lat, lon, nlayer]
    max_moist = np.rollaxis(da_max_moist.values, 0, 3)  # [lat, lon, nlayer]

    # Repeat data for each cell and layer for nveg*nsnow times, and convert
    # dimension to [lat, lon, n]
    max_moist_new = np.repeat(max_moist, nveg*nsnow).reshape([len(lat), len(lon), n])

    # Put into da
    da_max_moist_n = xr.DataArray(max_moist_new,
                                  coords=[lat, lon, range(n)],
                                  dims=['lat', 'lon', 'n'])

    return da_max_moist_n


def calculate_ensemble_mean_states(list_state_nc, out_state_nc):
    ''' Calculates ensemble-mean of multiple state files

    Parameters
    ----------
    list_state_nc: <list>
        A list of VIC state nc files whose mean to be calculated
    out_state_nc: <str>
        Path of output state netCDF file

    Returns
    ----------
    out_state_nc: <str>
        Path of output state netCDF file (same as input)

    Require
    ----------
    xarray
    '''

    # --- Number of files --- #
    N = len(list_state_nc)

    # --- Calculate ensemble mean (or median) for each state variable --- #
    list_ds = []
    for state_nc in list_state_nc:
        list_ds.append(xr.open_dataset(state_nc))
    ds_mean = list_ds[0].copy(deep=True)
    # STATE_SOIL_MOISTURE - mean
    ds_mean['STATE_SOIL_MOISTURE'] = (sum(list_ds) / N)['STATE_SOIL_MOISTURE']
    # STATE_SOIL_ICE - mean
    ds_mean['STATE_SOIL_ICE'] = (sum(list_ds) / N)['STATE_SOIL_ICE']
    # STATE_CANOPY_WATER - mean
    ds_mean['STATE_CANOPY_WATER'] = (sum(list_ds) / N)['STATE_CANOPY_WATER']
    # STATE_SNOW_AGE - median, integer
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_AGE'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_AGE'][:] = np.nanmedian(ar_var, axis=0).round()
    # STATE_SNOW_MELT_STATE - median, integer
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_MELT_STATE'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_MELT_STATE'][:] = np.nanmedian(ar_var, axis=0).round()
    # STATE_SNOW_COVERAGE - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_COVERAGE'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_COVERAGE'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_WATER_EQUIVALENT - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_WATER_EQUIVALENT'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_WATER_EQUIVALENT'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_SURF_TEMP - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_SURF_TEMP'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_SURF_TEMP'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_SURF_WATER - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_SURF_WATER'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_SURF_WATER'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_PACK_TEMP - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_PACK_TEMP'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_PACK_TEMP'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_PACK_WATER - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_PACK_WATER'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_PACK_WATER'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_DENSITY - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_DENSITY'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_DENSITY'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_COLD_CONTENT - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_COLD_CONTENT'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_COLD_CONTENT'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SNOW_CANOPY - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_SNOW_CANOPY'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_SNOW_CANOPY'][:] = np.nanmedian(ar_var, axis=0)
    # STATE_SOIL_NODE_TEMP - mean
    ds_mean['STATE_SOIL_NODE_TEMP'] = (sum(list_ds) / N)['STATE_SOIL_NODE_TEMP']
    # STATE_FOLIAGE_TEMPERATURE - mean
    ds_mean['STATE_FOLIAGE_TEMPERATURE'] = (sum(list_ds) / N)['STATE_FOLIAGE_TEMPERATURE']
    # STATE_ENERGY_LONGUNDEROUT - mean
    ds_mean['STATE_ENERGY_LONGUNDEROUT'] = (sum(list_ds) / N)['STATE_ENERGY_LONGUNDEROUT']
    # STATE_ENERGY_SNOW_FLUX - median
    list_var = []
    for ds in list_ds:
        list_var.append(ds['STATE_ENERGY_SNOW_FLUX'].values)
    ar_var = np.asarray(list_var)
    ds_mean['STATE_ENERGY_SNOW_FLUX'][:] = np.nanmedian(ar_var, axis=0)

    # Write to output netCDF file
    ds_mean.to_netcdf(out_state_nc, format='NETCDF4_CLASSIC')

    return out_state_nc


def run_vic_assigned_states(start_time, end_time, vic_exe, init_state_nc,
                            dict_assigned_state_nc, global_template,
                            vic_forcing_basepath, vic_model_steps_per_day,
                            output_global_root_dir,
                            output_vic_history_root_dir,
                            output_vic_log_root_dir, mpi_proc=None, mpi_exe=None):
    ''' Run VIC with assigned initial states and other assigned state files during the simulation time. All VIC runs do not output state file in the end.
    
    Parameters
    ----------
    start_time: <pandas.tslib.Timestamp>
        Start time of VIC run
    end_time: <pandas.tslib.Timestamp>
        End time of VIC run
    vic_exe: <class 'VIC'>
        VIC run class
    init_state_nc: <str>
        Path of initial state netCDF file; None for no initial state
    dict_assigned_state_nc: <OrderedDict>
        An ordered dictionary of state times and nc files after the start time;
        Keys: state times in <pandas.tslib.Timestamp>;
        Items: state netCDF file path in <str>
    global_template: <str>
        VIC global file template
    vic_forcing_basepath: <str>
        VIC forcing netCDF file basepath ('YYYY.nc' will be appended)
    vic_model_steps_per_day: <int>
        VIC model steps per day
    output_global_root_dir: <str>
        Directory for VIC global files
    output_vic_history_root_dir: <str>
        Directory for VIC output history files
    output_vic_log_root_dir: <str>
        Directory for VIC output log files
    mpi_proc: <int or None>
        Number of processors to use for VIC MPI run. None for not using MPI
    mpi_exe: <str>
        Path for MPI exe
    
    Returns
    ----------
    list_history_files: <list>
        A list of all output history file paths in order
    
    Require
    ----------
    OrderedDict
    generate_VIC_global_file
    check_returncode
    '''
    
    list_history_files = []  # A list of resulted history file paths
    
    # --- Run VIC from start_time to the first assigned state time --- #
    run_start_time = start_time
    run_end_time = list(dict_assigned_state_nc.keys())[0] - \
                   pd.DateOffset(hours=24/vic_model_steps_per_day)
    print('\tRunning VIC from ', run_start_time, 'to', run_end_time)
    propagate(start_time=run_start_time, end_time=run_end_time,
              vic_exe=vic_exe, vic_global_template_file=global_template,
              vic_model_steps_per_day=vic_model_steps_per_day,
              init_state_nc=init_state_nc,
              out_state_basepath=None,
              out_history_dir=output_vic_history_root_dir,
              out_history_fileprefix='history',
              out_global_basepath=os.path.join(output_global_root_dir, 'global'),
              out_log_dir=output_vic_log_root_dir,
              forcing_basepath=vic_forcing_basepath,
              mpi_proc=mpi_proc,
              mpi_exe=mpi_exe)
    list_history_files.append(os.path.join(
                    output_vic_history_root_dir,
                    'history.{}-{:05d}.nc'.format(
                            run_start_time.strftime('%Y-%m-%d'),
                            run_start_time.hour*3600+run_start_time.second)))
    
    # --- Run VIC from each assigned state time to the next (or to end_time) --- #
    for t, time in enumerate(dict_assigned_state_nc.keys()):
        # --- Determine last, current and next measurement time points --- #
        current_time = time
        if t == len(dict_assigned_state_nc)-1:  # if this is the last measurement time
            next_time = end_time
        else:  # if not the last measurement time
            next_time = list(dict_assigned_state_nc.keys())[t+1] - \
                        pd.DateOffset(hours=24/vic_model_steps_per_day)
        # If current_time > next_time, do not propagate (we already reach the end of the simulation)
        if current_time > next_time:
            break
        print('\tRunning VIC from ', current_time, 'to', next_time)
        
        # --- Propagate to the next time from assigned initial states --- #
        state_nc = dict_assigned_state_nc[current_time]
        propagate(start_time=current_time, end_time=next_time,
                  vic_exe=vic_exe, vic_global_template_file=global_template,
                  vic_model_steps_per_day=vic_model_steps_per_day,
                  init_state_nc=state_nc,
                  out_state_basepath=None,
                  out_history_dir=output_vic_history_root_dir,
                  out_history_fileprefix='history',
                  out_global_basepath=os.path.join(output_global_root_dir, 'global'),
                  out_log_dir=output_vic_log_root_dir,
                  forcing_basepath=vic_forcing_basepath,
                  mpi_proc=mpi_proc,
                  mpi_exe=mpi_exe)
        list_history_files.append(os.path.join(
                    output_vic_history_root_dir,
                    'history.{}-{:05d}.nc'.format(
                            current_time.strftime('%Y-%m-%d'),
                            current_time.hour*3600+current_time.second)))
        # --- Concat and delete individual history files for each year --- #
        # (If the end of EnKF run, or the end of a calendar year)
        if (t == (len(dict_assigned_state_nc.keys()) - 1)) or \
           (t < (len(dict_assigned_state_nc.keys()) - 1) and \
           current_time.year != (next_time + \
           pd.DateOffset(hours=24/vic_model_steps_per_day)).year):
            # Determine history file year
            year = current_time.year
            # Concat individual history files
            output_file=os.path.join(
                            output_vic_history_root_dir,
                            'history.concat.{}.nc'.format(year))
            concat_clean_up_history_file(list_history_files,
                                         output_file)
            # Reset history file list
            list_history_files = []


def rmse(true, est):
    ''' Calculates RMSE of an estimated variable compared to the truth variable

    Parameters
    ----------
    true: <np.array>
        A 1-D array of time series of true values
    est: <np.array>
        A 1-D array of time series of estimated values (must be the same length of true)

    Returns
    ----------
    rmse: <float>
        Root mean square error

    Require
    ----------
    numpy
    '''

    rmse = np.sqrt(sum((est - true)**2) / len(true))
    return rmse


def innov_norm_var(innov, y_est_before_update, R):
    ''' Calculates normalized variance of innovation
    
    Parameters
    ----------
    innov: <np.array>
        A 1-D array of time series of innovation (meas - y_est_before_update),
        averaged over all ensemble members
    y_est_before_update: <np.array>
        A 2-D array of y_est_before_update for all ensemble members
        Dim: [time, N]
    R: <float>
        Measurement error variance
    
    Returns
    ----------
    var_norm: <float>
        Normalized variance
    
    Require
    ----------
    numpy
    '''
    
    # If all nan, return a time series of nan
    if sum(~np.isnan(innov)) == 0:
        return np.nan
    
    # Pyy = cov(y, y.transpose); divided by (N-1)
    Pyy = np.cov(y_est_before_update).diagonal()  # [time]
    
    # Calculate normalized innovation time series
    innov_norm = innov / np.sqrt(Pyy + R)
    
    # Calculate variance
    var_norm = np.var(innov_norm)
    
    return var_norm


def load_nc_and_concat_var_years(basepath, start_year, end_year, dict_vars):
    ''' Loads in netCDF files end with 'YYYY.nc', and for each variable needed,
        concat all years together and return a DataArray
        
        Parameters
        ----------
        basepath: <str>
            Basepath of all netCDF files; 'YYYY.nc' will be appended;
            Time dimension name in the nc files must be 'time'
        start_year: <int>
            First year to load
        end_year: <int>
            Last year to load
        dict_vars: <dict>
            A dict of desired variables and corresponding varname in the
            netCDF files (e.g., {'prec': 'prcp'; 'temp': 'tair'}). The keys in
            dict_vars will be used as keys in the output dict.

        Returns
        ----------
        dict_da: <dict>
            A dict of concatenated xr.DataArrays.
            Keys: desired variables (using the same keys as in input
                  'dict_vars')
            Elements: <xr.DataArray>
    '''

    dict_list = {}
    for var in dict_vars.keys():
        dict_list[var] = []

    # Loop over each year
    for year in range(start_year, end_year+1):
        # Load data for this year
        ds = xr.open_dataset(basepath + '{}.nc'.format(year))
        # Extract each variable needed and put in a list
        for var, varname in dict_vars.items():
            da = ds[varname]
            # Put data of this year in a list
            dict_list[var].append(da)

    # Concat all years for all variables
    dict_da = {}
    for var in dict_vars.keys():
        dict_da[var] = xr.concat(dict_list[var], dim='time')

    return dict_da


def da_3D_to_2D_for_SMART(dict_da_3D, da_mask, time_varname='time'):
    ''' Convert data arrays with dimension [time, lat, lon] to [npixel_active, time]
    
    Parameters
    ----------
    dict_da_3D: <dict>
        A dict of xr.DataArray's with [time, lat, lon] dimension
        Keys: variable name
        Elements: xr.DataArray
    da_mask: <xr.DataArray>
        Domain mask (>0 for active grid cells)
    time_varname: <str>
        Varname for time in all da's in dict_da_3D
    
    Return
    ----------
    dict_array_active: <dict>
        Keys: variable name (same as in input)
        Elements: np.array with dimension [npixel_active, time]
    
    '''
    
    # Extract ntime, nlat and nlon
    da_example = dict_da_3D[list(dict_da_3D.keys())[0]]
    ntime = len(da_example[time_varname])
    nlat = len(da_example['lat'])
    nlon = len(da_example['lon'])
    
    # Reshape domain mask
    mask = da_mask.values
    mask_reshape = mask.reshape(nlat*nlon)
    
    # Convert all da's in dict_da_3D to [npixel_active, nday]
    dict_array_active = {}
    # Loop over each da
    for var, da in dict_da_3D.items():
        # Convert to np.array and reshape to [ntime, ncell]
        array_all_cells = da.values.reshape(ntime, nlat*nlon)
        # Only keep active grid cells to the final dimension [ntime, npixel_active]
        array_active = array_all_cells[:, mask_reshape>0]
        # Transpose dimension to [npixel_active, nday]
        array_active = np.transpose(array_active)
        # Put in final dictionary
        dict_array_active[var] = array_active
    
    return dict_array_active


def da_2D_to_3D_from_SMART(dict_array_2D, da_mask, out_time_varname, out_time_coord):
    ''' Convert data arrays with dimension [time, npixel_active] to [time, lat, lon]

    Parameters
    ----------
    dict_array_2D: <dict>
        Keys: variable name
        Elements: np.array with dimension [time, npixel_active]
    da_mask: <xr.DataArray>
        Domain mask (>0 for active grid cells)
    out_time_varname: <str>
        Varname for time in output xr.DataArray's
    out_time_coord: <list or other types usable for xr coords>
        Time coordinates in output xr.DataArray's

    Return
    ----------
    dict_da_3D: <dict>
        Keys: variable name (same as in input)
        Elements: xr.DataArray with dimension [time, lat, lon]

    '''

    # Extract ndays, nlat and nlon (as well as lat and lon)
    ntime = len(out_time_coord)
    lat = da_mask['lat']
    lon = da_mask['lon']
    nlat = len(lat)
    nlon = len(lon)    

    # Reshape domain mask and identify indices of active pixels
    mask = da_mask.values
    mask_reshape = mask.reshape(nlat*nlon)
    ind_active = np.where(mask_reshape>0)[0]
    
    # Convert all arrays in dict_da_2D to [time, lat, lon]
    dict_da_3D = {}
    # Loop over each da
    for var, array in dict_array_2D.items():
        # Fill in inactive cells (to dimension [time, ncells])
        array_allcells_reshape = np.empty([ntime, nlat*nlon])
        array_allcells_reshape[:] = np.nan
        array_allcells_reshape[:, ind_active] = array
        # Convert to dimension [time, lat, lon]
        array_3D = array_allcells_reshape.reshape([ntime, nlat, nlon])
        # Put in xr.DataArray
        da = xr.DataArray(array_3D, coords=[out_time_coord, lat, lon],
                          dims=[out_time_varname, 'lat', 'lon'])
        # Put in final dictionary
        dict_da_3D[var] = da

    return dict_da_3D


def correct_prec_from_SMART(da_prec_orig, window_size, da_prec_corr_window,
                            start_date):
    ''' Correct (i.e., rescale) original precipitation data based on outputs from
        SMART (which is corrected prec sum in longer time windows).
            - The first window is not corrected (orig. prec used)
            - The last incomplete window, if exists, is not corrected (orig. prec used)
        
    Parameters
    ----------
    da_prec_orig: <xr.DataArray>
        Original prec data to be corrected. Original timestep, typically daily or
        sub-daily.
        Dims: [time, lat, lon]
    window_size: <int>
        Size of each window [unit: day]
    da_prec_corr_window: <xr.DataArray>
        Corrected prec sum in each window
        Dims: [window (index starting from 0), lat, lon]
        NOTE: this is the output from SMART:
                - the first window data is junk (always zeros)
                - Only include complete windows
    start_date: <dt.datetime or pd.datetime>
        Starting date (midnight) of the starting time of all the time series
        
    Returns
    ----------
    da_prec_corrected: <xr.DataArray>
        Corrected prec data to be corrected. Original timestep, consistent with that
        of input da_prec_orig
        Dims: [time, lat, lon]
    '''
    
    # Identify the number of complete windows
    nwindow = len(da_prec_corr_window['window'])
    
    # Sum original prec in each window (the last window may be incomplete)
    da_prec_orig_window = da_prec_orig.resample(
                            dim='time',
                            freq='{}D'.format(window_size),
                            how='sum')
    da_prec_orig_window = da_prec_orig_window.rename({'time': 'window'})
    
    # Loop over each window and rescale original prec (skip first window)
    da_prec_corrected = da_prec_orig.copy(deep=True)
    for i in range(1, nwindow):
        # --- Select out this window period from original prec data --- #
        window_start_date = start_date + pd.DateOffset(days=i*window_size)
        window_end_date = start_date + pd.DateOffset(days=(i+1)*window_size-0.0001)
        da_prec_orig_this_window = da_prec_orig.sel(
                time=slice(window_start_date, window_end_date))
        # --- Rescale --- #
        # (1) Calculate rescaling factors for this window;
        prec_corr = da_prec_corr_window[i, :, :].values  # dim: [lat, lon]
        prec_sum_orig = da_prec_orig_window[i, :, :].values  # dim: [lat, lon]
        # Note: If prec_sum_orig is 0 for a grid cell, scale_factors will be np.inf for
        # that grid cell
        scale_factors = prec_corr / prec_sum_orig
        # (2) Rescale for the orig. prec data (at orig. sub-daily or daily timestep)
        # for this window (here array broadcast is used)
        prec_corr_this_window = da_prec_orig_this_window.values * scale_factors
        # (3) For grid cells where scale_factors == np.inf or scale == np.nan
        # (which indicates orig. prec are all zero for this window and this
        # cell; np.inf indicates non-zero numerator while np.nan indicates
        # zero numerator; np.nan at inactive grid cells as well), fill in with
        # constant corrected prec from SMART
        # Index of zero-orig-prec grid cells
        ind_inf_scale = np.where(np.isinf(scale_factors) + np.isnan(scale_factors))
        # Constant corrected prec to fill in (divided to each orig. timestep)
        const_prec = prec_corr / len(da_prec_orig_this_window['time'])
        # Fill in those zero-orig-prec grid cells
        const_prec_inf_scale = const_prec[ind_inf_scale[0], ind_inf_scale[1]]
        prec_corr_this_window[:, ind_inf_scale[0], ind_inf_scale[1]] = const_prec_inf_scale
        # --- Put in the final da --- #
        da_prec_corrected.sel(time=slice(window_start_date, window_end_date))[:] = \
                prec_corr_this_window
    
    return da_prec_corrected


def calculate_rmse_prec(out_nc, da_truth, da_model,
                        agg_freq, log):
    ''' A wrap funciton that calculates RMSE for all domain and save to file; if
        result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        RMSE result output netCDF file
    da_truth: <xr.Dataset>
        Truth precip
    da_model: <xr.Dataset>
        Corrected prec whose RMSE is to be assessed (wrt. truth)
    agg_freq: <str>
        Aggregation frequency for rmse calculation;
        e.g., '3H', '3D'
    log: <bool>
        Whether to take log first before calculating RMSE (but after aggregating)
    '''

    if not os.path.isfile(out_nc):  # if RMSE is not already calculated
        # --- Aggregate --- #
        da_truth_agg = da_truth.resample(dim='time', how='sum', freq=agg_freq)
        da_model_agg = da_model.resample(dim='time', how='sum', freq=agg_freq)
        # --- Take log if specified --- #
        if log is True:
            da_truth_agg = np.log(da_truth_agg + 1)
            da_model_agg = np.log(da_model_agg + 1)
        # --- Calculate RMSE --- #
        # Determine the total number of loops
        lat_coord = da_truth['lat']
        lon_coord = da_truth['lon']
        nloop = len(lat_coord) * len(lon_coord)
        # Reshape variables
        truth = da_truth_agg.values.reshape(
            [len(da_truth_agg['time']), nloop])  # [time, nloop]
        model = da_model_agg.values.reshape(
            [len(da_model_agg['time']), nloop])  # [time, nloop]
        # Calculate RMSE for all grid cells
        rmse_model = np.array(list(map(
                     lambda j: rmse(truth[:, j], model[:, j]),
                    range(nloop))))  # [nloop]
        # Reshape RMSE's
        rmse_model = rmse_model.reshape(
            [len(lat_coord), len(lon_coord)])  # [lat, lon]
        # Put results into da's
        da_rmse_model = xr.DataArray(
            rmse_model, coords=[lat_coord, lon_coord],
            dims=['lat', 'lon'])  # [mm/mm]
        # Save RMSE to netCDF file
        ds_rmse_model = xr.Dataset(
            {'rmse': da_rmse_model})
        ds_rmse_model.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if RMSE is already calculated
        da_rmse_model = xr.open_dataset(out_nc)['rmse']

    return da_rmse_model


def calculate_corrcoef_prec(out_nc, da_truth, da_model,
                            agg_freq, log):
    ''' A wrap funciton that calculates corrcoef for all domain and save to file; if
        result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        corrcoef result output netCDF file
    da_truth: <xr.Dataset>
        Truth precip
    da_model: <xr.Dataset>
        Corrected prec whose corrcoef is to be assessed (wrt. truth)
    agg_freq: <str>
        Aggregation frequency for corrcoef calculation;
        e.g., '3H', '3D'
    log: <bool>
        Whether to take log first before calculating corrcoef (but after aggregating)
    '''

    if not os.path.isfile(out_nc):  # if corrcoef is not already calculated
        # --- Aggregate --- #
        da_truth_agg = da_truth.resample(dim='time', how='sum', freq=agg_freq)
        da_model_agg = da_model.resample(dim='time', how='sum', freq=agg_freq)
        # --- Take log if specified --- #
        if log is True:
            da_truth_agg = np.log(da_truth_agg + 1)
            da_model_agg = np.log(da_model_agg + 1)
        # --- Calculate corrcoef --- #
        # Determine the total number of loops
        lat_coord = da_truth['lat']
        lon_coord = da_truth['lon']
        nloop = len(lat_coord) * len(lon_coord)
        # Reshape variables
        truth = da_truth_agg.values.reshape(
            [len(da_truth_agg['time']), nloop])  # [time, nloop]
        model = da_model_agg.values.reshape(
            [len(da_model_agg['time']), nloop])  # [time, nloop]
        # Calculate corrcoef for all grid cells
        corrcoef_model = np.array(list(map(
                     lambda j: np.corrcoef(truth[:, j], model[:, j])[0, 1],
                    range(nloop))))  # [nloop]
        # Reshape corrcoef's
        corrcoef_model = corrcoef_model.reshape(
            [len(lat_coord), len(lon_coord)])  # [lat, lon]
        # Put results into da's
        da_corrcoef_model = xr.DataArray(
            corrcoef_model, coords=[lat_coord, lon_coord],
            dims=['lat', 'lon'])  # [mm/mm]
        # Save corrcoef to netCDF file
        ds_corrcoef_model = xr.Dataset(
            {'corrcoef': da_corrcoef_model})
        ds_corrcoef_model.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if corrcoef is already calculated
        da_corrcoef_model = xr.open_dataset(out_nc)['corrcoef']

    return da_corrcoef_model


def calculate_categ_metrics(da_threshold, da_truth, da_model, agg_freq):
    ''' Calculates FAR, POD and TS
    
    Parameters
    ----------
    da_truth: <xr.Dataset>
        Truth precip
    da_model: <xr.Dataset>
        Corrected prec
    agg_freq: <str>
        Aggregation frequency for corrcoef calculation;
        e.g., '3H', '3D'
    da_threshold: <xr.DataArray>
        Prec event threshold (mm/agg_period)
    
    Returns
    ----------
    da_far, da_pod, da_ts: <xr.DataArray>
        2D FAR, POD and TS
    '''
    
    # --- Aggregate --- #
    da_truth_agg = da_truth.resample(dim='time', how='sum', freq=agg_freq)
    da_model_agg = da_model.resample(dim='time', how='sum', freq=agg_freq)
    # --- Calculate H (true pos), F (false pos) and M (missed event) --- #
    da_truth_pos = da_truth_agg > da_threshold
    da_model_pos = da_model_agg > da_threshold
    H = (da_truth_pos & da_model_pos).sum(dim='time')
    F = ((~da_truth_pos) & da_model_pos).sum(dim='time')
    M = (da_truth_pos & (~da_model_pos)).sum(dim='time')
    # --- Calculate metrics --- #
    da_far = F / (H + F)
    da_pod = H / (H + M)
    da_ts = H / (H + F + M)
    
    return da_far, da_pod, da_ts


def calculate_prec_threshold(out_nc, perc, da_prec, agg_freq):
    ''' A wrap funciton that calculates percentile threshold for all domain and save to file; if
        result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        RMSE result output netCDF file
    perc: <float> (0-100)
        Percentile threshold to distinguish rainfall event
    da_prec: <xr.Dataset>
        Prec data (first dim must be time)
    agg_freq: <str>
        Aggregation frequency for rmse calculation;
        e.g., '3H', '3D'
    
    Returns
    ----------
    da_threshold: <xr.DataArray>
        Prec event threshold (mm/agg_period)
    '''

    if not os.path.isfile(out_nc):  # if not already calculated
        # --- Aggregate --- #
        da_prec_agg = da_prec.resample(dim='time', how='sum', freq=agg_freq)
        # --- Calculate threshold --- #
        threshold = np.nanpercentile(
            da_prec_agg.where(da_prec_agg>0).values,
            q=perc, axis=0)
        # Put results into da's
        lat_coord = da_prec['lat']
        lon_coord = da_prec['lon']
        da_threshold = xr.DataArray(
            threshold, coords=[lat_coord, lon_coord],
            dims=['lat', 'lon'])  # [mm/agg_period]
        # Save RMSE to netCDF file
        ds_threshold = xr.Dataset(
            {'threshold': da_threshold})
        ds_threshold.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if RMSE is already calculated
        da_threshold = xr.open_dataset(out_nc)['threshold']

    return da_threshold


def calculate_crps_prec(out_nc, da_truth, da_model, log=False, nproc=1):
    ''' A wrap funciton that calculates CRPS for all domain and save to file; if
        result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        RMSE result output netCDF file
    ds_truth: <xr.DataArray>
        Truth states/history
    da_model: <xr.DataArray>
        Model states/history whose RMSE is to be assessed (wrt. truth states);
        This should be ensemble model results, with "N" as the ensemble dimension
    log: <bool>
        Whether to take log first before calculating RMSE
    nproc: <int>
        Number of processors for mp

    Returns
    ----------
    da_crps: <xr.DataArray>
        CRPS for the whole domain; dimension: [lat, lon]
    '''

    if not os.path.isfile(out_nc):  # if RMSE is not already calculated
        # --- Take log if specified --- #
        if log is True:
            da_truth = np.log(da_truth + 1)
            da_model = np.log(da_model + 1)
        # --- Calculate CRPS for the whole domain --- #
        results = {}
        pool = mp.Pool(processes=nproc)
        for lat in da_truth['lat'].values:
            for lon in da_truth['lon'].values:
                results[(lat, lon)] = pool.apply_async(
                    crps, (da_truth.sel(lat=lat, lon=lon).values,
                           da_model.sel(lat=lat, lon=lon).transpose('time', 'N').values))
        pool.close()
        pool.join()
        # --- Get return values --- #
        crps_domain = np.zeros([len(da_truth['lat']), len(da_truth['lon'])])
        crps_domain[:] = np.nan
        da_crps = xr.DataArray(
            crps_domain, coords=[da_truth['lat'], da_truth['lon']],
            dims=['lat', 'lon'])
        for i, result in results.items():
            lat = i[0]
            lon = i[1]
            da_crps.loc[lat, lon] = result.get()
        # Save CRPS to netCDF file
        ds_crps = xr.Dataset(
            {'crps': da_crps})
        ds_crps.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if already calculated
        da_crps = xr.open_dataset(out_nc)['crps']

    return da_crps


def crps(truth, ensemble):
    ''' Calculate mean CRPS of an ensemble time series
    Parameters
    ----------
    truth: <np.array>
        A 1-D array of truth time series
        Dimension: [n]
    ensemble: <np.array>
        A 2-D array of ensemble time series
        Dimension: [n, N], where N is ensemble size; n is time series length

    Returns
    ----------
    crps: <float>
        Time-series-mean CRPS

    Require
    ----------
    import properscoring as ps
    '''

    array_crps = np.asarray([ps.crps_ensemble(truth[t], ensemble[t, :]) for t in range(len(truth))])
    crps = array_crps.mean()

    return crps


def nensk(truth, ensemble):
    ''' Calculate the ratio of temporal-mean ensemble skill to temporal-mean ensemble spread:
            nensk = <ensk> / <ensp>
    where <ensk> is temporal average of: ensk(t) = (ensmean - truth)^2
          <ensp> is temperal average of: ensp(t) = mean((ens_i - ensmean)^2) = var(ens_i)

    Parameters
    ----------
    truth: <np.array>
        A 1-D array of truth time series
        Dimension: [n]
    ensemble: <np.array>
        A 2-D array of ensemble time series
        Dimension: [n, N], where N is ensemble size; n is time series length

    Returns
    ----------
    nensk: <float>
        Normalized ensemble skill
    '''

    ensk = np.square((ensemble.mean(axis=1) - truth))  # [n]
    ensp = ensemble.var(axis=1)  # [n]
    nensk = np.mean(ensk) / np.mean(ensp)

    return nensk


def calculate_nensk(out_nc, da_truth, da_model, log):
    ''' A wrap funciton that calculates NENSK for all domain
    and save to file; if result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        RMSE result output netCDF file
    da_truth: <xr.DataArray>
        Truth states/history
    da_model: <xr.DataArray>
        Model states/history whose NENSK is to be assessed (wrt. truth states);
        This should be ensemble model results, with "N" as the ensemble dimension
        NOTE: this should already be daily data!!
    log: <bool>
        True or False; whether to take log transformation

    Returns
    ----------
    da_bias_norm_var: <xr.DataArray>
        Variance of ensemble-normalized bias for the whole domain; dimension: [lat, lon]
    '''

    if not os.path.isfile(out_nc):  # if RMSE is not already calculated
        # --- Take log if specified --- #
        if log is True:
            da_truth = np.log(da_truth + 1)
            da_model = np.log(da_model + 1)
        # --- Calculate nensk for the whole domain --- #
        nensk_domain = np.asarray(
            [nensk(
                da_truth.sel(lat=lat, lon=lon).values,
                da_model.sel(lat=lat, lon=lon).transpose('time', 'N').values)
             for lat in da_truth['lat'].values
             for lon in da_truth['lon'].values])
        # --- Reshape results --- #
        nensk_domain = nensk_domain.reshape(
            [len(da_truth['lat']), len(da_truth['lon'])])
        # --- Put results into da's --- #
        da_nensk = xr.DataArray(
            nensk_domain, coords=[da_truth['lat'], da_truth['lon']],
            dims=['lat', 'lon'])
        # Save RMSE to netCDF file
        ds_nensk = xr.Dataset(
            {'nensk': da_nensk})
        ds_nensk.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if RMSE is already calculated
        da_nensk = xr.open_dataset(out_nc)['nensk']

    return da_nensk


def get_z_values(ens, obs):
    ''' Calculate cumulative probability corresponding to observed 
        flow (F(obs)) from forecast ensemble CDF (F(x)).
        These are z_i in Laio and Tamea 2007 Fig. 2.
        https://doi.org/10.5194/hess-11-1267-2007
        Input: ens = ensemble forecast for a single day
                     numpy.array, num_ensemble_members x 1 
               obs = observation for a single day, one value.
        Output: z = cumulative probability of observation based on
                    forecast ensemble, single value
    '''
#    # construct forecast CDF from ensemble weights
#    cdf, ens_sort = construct_cdf(ens)
#    # get z = F(obs) from forecast CDF
#    z = cdf_simulated(obs, cdf, ens_sort)

    if obs < min(ens):
        z = 0
    elif obs > max(ens):
        z = 1
    else:
        z = stats.percentileofscore(ens, obs, 'mean') / 100
    return z


def get_z_values_timeseries(ens_ts, obs_ts):
    ''' Calculate z values for a time series of ensemble
    Parameters
    ----------
    ens_ts: <np.array>
        An ensemble of timeseries
        dim: [N, t] where N is ensemble size and t is timesteps
    obs_ts: <np.array>
        Time series of observations
        dim: [t]
        
    Returns
    ----------
    z_alltimes: <np.array>
        Quantile of observation in ensemble at all time series
        dim: [t]
    '''
    
    if len(obs_ts) != ens_ts.shape[1]:
        raise ValueError('Ensemble and observed time series not the same length!')
    
    z_alltimes = \
        np.asarray(
            [get_z_values(ens_ts[:, t], obs_ts[t])
             for t in range(len(obs_ts))])
        
    return z_alltimes


def calc_reliability_bias(z_daily):
    ''' Calculate alpha-reliability following Renard et al. 2010
        (Eqn. 23) https://doi.org/11.1029/2009WR008328. See also
        Laio and Tamea 2007
        https://doi.org/10.5194/hess-11-1267-2007
        Inputs: z_daily = daily z = F(obs) values. These correspond to
                          "quantile of observed p-values" from
                          Renard et al. 2010 Fig. 3,
                          "observed p-values of x_t" from Renard et
                          al. 2010 Eqn. 23b, and z_i from Laio and 
                          Tamea 2007 Fig. 2.
                          numpy.array 1 x num_days
        Outputs: alpha_reliability
                Range: -1 to 1; best-performance value = 0
                positive value indicates over-prediction
                negative value indicates under-prediction
    '''
    
    z_daily = np.sort(z_daily)
    n_sample = len(z_daily)
    # assign ranks to daily z = F(obs) values.
    # rank/# samples gives us the "theoretical quantile of U[0,1]"
    # from Renard et al. 2010 Fig. 3, "theoretical p-values of x_t" 
    # from Renard et al. 2010 Eqn. 23b, and R_i/n from Laio and Tamea 
    # 2007 Fig. 2.
    # Note: Renard et al. 2010 Fig.3 flipped the x and y axes from 
    # Laio and Tamea 2007 Fig. 2.
    R = np.arange(0, n_sample) / n_sample
    # calculate alpha reliability index
    alpha_direction = - 2 * np.mean(z_daily - R)
    return alpha_direction


def calc_alpha_reliability(z_daily):
    ''' Calculate alpha-reliability following Renard et al. 2010
        (Eqn. 23) https://doi.org/11.1029/2009WR008328. See also
        Laio and Tamea 2007
        https://doi.org/10.5194/hess-11-1267-2007
        Inputs: z_daily = daily z = F(obs) values. These correspond to
                          "quantile of observed p-values" from
                          Renard et al. 2010 Fig. 3,
                          "observed p-values of x_t" from Renard et
                          al. 2010 Eqn. 23b, and z_i from Laio and 
                          Tamea 2007 Fig. 2.
                          numpy.array 1 x num_days
        Outputs: alpha = alpha reliability index, single value
    '''
    
    z_daily = np.sort(z_daily)
    n_sample = len(z_daily)
    # assign ranks to daily z = F(obs) values.
    # rank/# samples gives us the "theoretical quantile of U[0,1]"
    # from Renard et al. 2010 Fig. 3, "theoretical p-values of x_t" 
    # from Renard et al. 2010 Eqn. 23b, and R_i/n from Laio and Tamea 
    # 2007 Fig. 2.
    # Note: Renard et al. 2010 Fig.3 flipped the x and y axes from 
    # Laio and Tamea 2007 Fig. 2.
    R = np.arange(0, n_sample) / n_sample
    # calculate alpha reliability index
    alpha = 1 - 2 * np.mean(abs(z_daily - R))
    return alpha


def calc_alpha_reliability_domain(out_nc, dict_z_domain, da_mask):
    ''' A wrap funciton that calculates alpha reliability for all domain
    and save to file; if result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        RMSE result output netCDF file
    dict_z_domain: <dict>
        {lat_lon: array of z values}
    da_mask: <xr.DataArray>
        Domain mask file; dim: [lat, lon]

    Returns
    ----------
    da_alpha: <xr.DataArray>
        alpha reliability for the whole domain; dimension: [lat, lon]
    '''

    if not os.path.isfile(out_nc):  # if not already calculated
        # --- Calculate for the whole domain --- #
        alpha_domain = np.asarray(
            [calc_alpha_reliability(dict_z_domain['{}_{}'.format(lat, lon)])
             if '{}_{}'.format(lat, lon) in dict_z_domain.keys() else np.nan
             for lat in da_mask['lat'].values
             for lon in da_mask['lon'].values])
        # --- Reshape results --- #
        alpha_domain = alpha_domain.reshape(
            [len(da_mask['lat']), len(da_mask['lon'])])
        # --- Put results into da's --- #
        da_alpha = xr.DataArray(
            alpha_domain, coords=[da_mask['lat'], da_mask['lon']],
            dims=['lat', 'lon'])
        # Save to netCDF file
        ds_alpha = xr.Dataset(
            {'alpha': da_alpha})
        ds_alpha.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if RMSE is already calculated
        da_alpha = xr.open_dataset(out_nc)['alpha']

    return da_alpha


def calculate_z_value_prec_domain(out_pickle, da_truth, da_model, nproc=1):
    ''' A wrap funciton that calculates z value for all domain and save to file; if
        result file already existed, then simply read in the file.
        NOTE: if at a timestep all ensemble members AND obs are zero, this timestep
        will be excluded from z-value calculation

    Parameters
    ----------
    out_pickle: <str>
        Output pickle file
    ds_truth: <xr.DataArray>
        Truth states/history
    da_model: <xr.DataArray>
        Model states/history whose RMSE is to be assessed (wrt. truth states);
        This should be ensemble model results, with "N" as the ensemble dimension
    nproc: <int>
        Number of processors for mp

    Returns
    ----------
    dict_z_domain: <dict>
        {lat_lon: array of z values}
    '''

    if not os.path.isfile(out_pickle):  # if not already calculated
        # --- Calculate CRPS for the whole domain --- #
        pool = mp.Pool(processes=nproc)
        results = {}
        for lat in da_truth['lat'].values:
            for lon in da_truth['lon'].values:
                # Exclude timesteps wit all-zero ensemble and truth
                ens =  da_model.sel(lat=lat, lon=lon).transpose('N', 'time').values
                truth = da_truth.sel(lat=lat, lon=lon).values
                valid_timesteps = ((ens>0).sum(axis=0) > 0) | (truth > 0)
                ens_valid = ens[:, valid_timesteps]
                truth_valid = truth[valid_timesteps]
                # Calculate z value time series
                results[(lat, lon)] = pool.apply_async(
                    get_z_values_timeseries, 
                    (ens_valid, truth_valid))
        pool.close()
        pool.join()
        # --- Get return values --- #
        dict_z_domain = {}  # {lat_lon: array}
        for i, result in results.items():
            lat = i[0]
            lon = i[1]
            dict_z_domain['{}_{}'.format(lat, lon)] = result.get()
        # Save z values using pickle
        with open(out_pickle, 'wb') as f:
            pickle.dump(dict_z_domain, f)
    else:  # if already calculated
        with open(out_pickle, 'rb') as f:
            dict_z_domain = pickle.load(f)

    return dict_z_domain


def calc_kesi_domain(out_nc, dict_z_domain, da_mask):
    ''' A wrap funciton that calculates kesi (fraction of observed
    timesteps within the ensemble range) for all domain
    and save to file; if result file already existed, then simply read in the file.

    Parameters
    ----------
    out_nc: <str>
        result output netCDF file
    dict_z_domain: <dict>
        {lat_lon: array of z values}
    da_mask: <xr.DataArray>
        Domain mask file; dim: [lat, lon]

    Returns
    ----------
    da_kesi: <xr.DataArray>
        kesi for the whole domain; dimension: [lat, lon]
    '''

    if not os.path.isfile(out_nc):  # if not already calculated
        # --- Calculate for the whole domain --- #
        kesi_domain = np.asarray(
            [calc_kesi(dict_z_domain['{}_{}'.format(lat, lon)])
             if '{}_{}'.format(lat, lon) in dict_z_domain.keys() else np.nan
             for lat in da_mask['lat'].values
             for lon in da_mask['lon'].values])
        # --- Reshape results --- #
        kesi_domain = kesi_domain.reshape(
            [len(da_mask['lat']), len(da_mask['lon'])])
        # --- Put results into da's --- #
        da_kesi = xr.DataArray(
            kesi_domain, coords=[da_mask['lat'], da_mask['lon']],
            dims=['lat', 'lon'])
        # Save to netCDF file
        ds_kesi = xr.Dataset(
            {'kesi': da_kesi})
        ds_kesi.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
    else:  # if RMSE is already calculated
        da_kesi = xr.open_dataset(out_nc)['kesi']

    return da_kesi


def calc_kesi(z_alltimes):
    ''' Calculate kesi (fraction of observed timesteps within the
        ensemble range)
        
    Parameters
    ----------
    z_alltimes: <np.array>
        z values of all timesteps; dim: [time]
        
    Returns
    ----------
    kesi: <float>
        kesi
    '''
    
    kesi = 1 - ((z_alltimes==1).sum() + (z_alltimes==0).sum()) \
        / len(z_alltimes)
        
    return kesi




