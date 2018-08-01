
''' This script plots SMART ensemble statistics only, including:

    Usage:
        $ python plot_SMART_results.py <config_file_SMART>
'''

import xarray as xr
import sys
import os
import datetime as dt
import pandas as pd
import numpy as np
from scipy.io import savemat, loadmat

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from bokeh.plotting import figure, output_file, save
from bokeh.io import reset_output
import bokeh
import warnings
warnings.filterwarnings('ignore')

from tonic.io import read_configobj

from da_utils import (load_nc_and_concat_var_years, setup_output_dirs,
                      da_3D_to_2D_for_SMART, da_2D_to_3D_from_SMART, rmse,
                      to_netcdf_forcing_file_compress, calculate_rmse_prec,
                      calculate_corrcoef_prec, calculate_categ_metrics,
                      calculate_prec_threshold, calculate_crps_prec)


# ============================================================ #
# Process command line arguments
# Read config file
# ============================================================ #
cfg = read_configobj(sys.argv[1])


# ============================================================ #
# Check wether PLOT section is in the cfg file
# ============================================================ #
if 'PLOT' in cfg:
    pass
else:
    raise ValueError('Must have [PLOT] section in the cfg file to plot'
                     'SMART-corrected rainfall results!')


# ============================================================ #
# Process some input variables
# ============================================================ #
start_time = pd.to_datetime(cfg['SMART_RUN']['start_time'])
end_time = pd.to_datetime(cfg['SMART_RUN']['end_time'])
start_year = start_time.year
end_year = end_time.year
time_step = cfg['SMART_RUN']['time_step']  # [hour]
window_size = cfg['SMART_RUN']['window_size']  # number of timesteps


# ============================================================ #
# Set up output directory
# ============================================================ #
output_dir = setup_output_dirs(
                    os.path.join(cfg['CONTROL']['root_dir'],
                                 cfg['OUTPUT']['output_basedir']),
                    mkdirs=['plots.{}'.format(cfg['PLOT']['smart_output_from'])])\
             ['plots.{}'.format(cfg['PLOT']['smart_output_from'])]

output_subdir_maps = setup_output_dirs(
                            output_dir,
                            mkdirs=['maps'])['maps']
output_subdir_ts = setup_output_dirs(
                            output_dir,
                            mkdirs=['time_series'])['time_series']
output_subdir_data = setup_output_dirs(
                            output_dir,
                            mkdirs=['data'])['data']


# ============================================================ #
# Load data
# ============================================================ #
print('Load data...')

# --- Load origigal precip --- #
print('\tOriginal precip')
da_prec_orig = load_nc_and_concat_var_years(
    basepath=os.path.join(
        cfg['CONTROL']['root_dir'],
        cfg['PLOT']['orig_prec_basepath']),
    start_year=start_year,
    end_year=end_year,
    dict_vars={'prec_orig': cfg['PLOT']['orig_prec_varname']})\
        ['prec_orig'].sel(time=slice(start_time, end_time))
    
# --- Load truth precip --- #
print('\tTrue precip')
da_prec_truth = load_nc_and_concat_var_years(
    basepath=os.path.join(
        cfg['CONTROL']['root_dir'],
        cfg['PLOT']['truth_prec_basepath']),
    start_year=start_year,
    end_year=end_year,
    dict_vars={'prec_truth': cfg['PLOT']['truth_prec_varname']})\
        ['prec_truth'].sel(time=slice(start_time, end_time))

# --- Load perturbed and SMART-corrected precip --- #
# Identify which SMART postprocessed directory to read from
if cfg['PLOT']['smart_output_from'] == 'post':
    smart_outdir = os.path.join(
        cfg['CONTROL']['root_dir'], cfg['OUTPUT']['output_basedir'], 'post_SMART')
elif cfg['PLOT']['smart_output_from'] == 'spatial_downscale':
    smart_outdir = os.path.join(
        cfg['CONTROL']['root_dir'], cfg['OUTPUT']['output_basedir'], 'post_spatial_downscaled')
elif cfg['PLOT']['smart_output_from'] == 'remap':
    smart_outdir = os.path.join(
        cfg['CONTROL']['root_dir'], cfg['OUTPUT']['output_basedir'], 'post_final_remapped')
# Load ensemble results (post only)
list_freq = ['3H', '1D', '3D']
filter_flag = cfg['SMART_RUN']['filter_flag']
if (filter_flag == 2 or filter_flag == 6) and cfg['PLOT']['smart_output_from'] == 'post':
    print('\tSMART-corrected, ensemble...')
    dict_da_prec_allEns = {}  # {freq: prec_type: da}
    for freq in list_freq:
        dict_da_prec_allEns[freq] = {}
        for prec_type in ['perturbed', 'corrected']:
            print('\t{}, {}'.format(freq, prec_type))
            out_nc = os.path.join(output_subdir_data,
                                  'prec_{}_allEns.{}.nc'.format(prec_type, freq))
            if not os.path.isfile(out_nc):  # if not already loaded
                list_da_prec = []
                for i in range(cfg['SMART_RUN']['NUMEN']):
                    print('\tEnsemble {}'.format(i+1))
                    if freq == '{}H'.format(time_step):  # if the native SMART timestep
                        da_prec = load_nc_and_concat_var_years(
                            basepath=os.path.join(smart_outdir, 'prec_{}.ens{}.'.format(prec_type, i+1)),
                            start_year=start_year,
                            end_year=end_year,
                            dict_vars={'prec': 'prec_corrected'})['prec'].sel(
                                time=slice(start_time, end_time))
                    else:  # if not the SMART timestep, should have already pre-aggregated precip data
                        da_prec = xr.open_dataset(
                            os.path.join(smart_outdir,
                                         'prec_{}.ens{}.{}_{}.nc'.format(prec_type, i+1, start_year, end_year)))['PREC']
                    list_da_prec.append(da_prec)
                # Concat all ensemble members
                da_prec_allEns = xr.concat(list_da_prec, dim='N')
                da_prec_allEns['N'] = range(cfg['SMART_RUN']['NUMEN'])
                # Save to file
                ds_prec_allEns = xr.Dataset({'PREC': da_prec_allEns})
                ds_prec_allEns.to_netcdf(out_nc, format='NETCDF4_CLASSIC')
            else: # if alreay loaded
                da_prec_allEns = xr.open_dataset(out_nc)['PREC']
            # Put the concat da into dict
            dict_da_prec_allEns[freq][prec_type] = da_prec_allEns

# --- Domain mask --- #
# Domain for plotting
da_mask = xr.open_dataset(os.path.join(
                cfg['CONTROL']['root_dir'],
                cfg['PLOT']['domain_nc']))['mask']
# Domain for SMART run
da_mask_smart = xr.open_dataset(os.path.join(
                cfg['CONTROL']['root_dir'],
                cfg['DOMAIN']['domain_file']))['mask']


# ============================================================ #
# Plot CRPS and PSR (right now for the SMART-resolution only)
# ============================================================ #
if cfg['PLOT']['smart_output_from'] == 'post':
    print('Plotting CRPS and PSR...')
    list_freq = ['3H', '1D', '3D']
    # --- Load CRPS (should have already been calculated for speedup) --- #
    for freq in list_freq:
        for log in [True, False]:
            dict_da_crps = {}
            for prec_type in ['perturbed', 'corrected']:
                print('\t{}, {}'.format(freq, prec_type))
                if log is True:
                    out_nc = os.path.join(output_subdir_data, 'crps_log.{}.{}.nc'.format(freq, prec_type))
                else:
                    out_nc = os.path.join(output_subdir_data, 'crps.{}.{}.nc'.format(freq, prec_type))
                da_crps = calculate_crps_prec(
                    out_nc, da_prec_truth, dict_da_prec_allEns[freq][prec_type],
                    log=log, nproc=1).where(da_mask)
                dict_da_crps[prec_type] = da_crps
            # --- Calculate PER --- #
            da_psr = (1 - dict_da_crps['corrected'] / dict_da_crps['perturbed']) * 100

            # --- Plot PSR --- #
            # CRPS of perturbed and corrected precipitation
            for prec_type in ['perturbed', 'corrected']:
                fig = plt.figure(figsize=(14, 7))
                cs = dict_da_crps[prec_type].plot(add_colorbar=False, cmap='viridis', vmin=0, vmax=5)
                cbar = plt.colorbar(cs, extend='max').set_label('CRPS', fontsize=20)
                plt.title('CRPS of {} precip., {}\n'
                          'domain median = {:.2f}'.format(
                                prec_type, freq,
                                dict_da_crps[prec_type].median().values), fontsize=20)
                prefix = 'crps_log' if log is True else 'crps'
                fig.savefig(os.path.join(output_subdir_maps, '{}.{}.{}.png'.format(
                    prefix, freq, prec_type)),
                        format='png',
                        bbox_inches='tight', pad_inches=0)
            # PSR
            fig = plt.figure(figsize=(14, 7))
            cs = da_psr.plot(
                add_colorbar=False, cmap='bwr_r', vmin=-40, vmax=40)
            cbar = plt.colorbar(cs, extend='both').set_label('Precent CRPS reduction (%)', fontsize=20)
            log_title = 'log precip.' if log is True else 'raw precip.'
            plt.title('Percent CRPS reduction (PER), {} {}\n'
                'domain median = {:.1f}%'.format(log_title, freq, da_psr.median().values),
                fontsize=20)
            prefix = 'psr_log' if log is True else 'psr'
            fig.savefig(os.path.join(output_subdir_maps, '{}.{}.png'.format(prefix, freq)), format='png',
                bbox_inches='tight', pad_inches=0)


