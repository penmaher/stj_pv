# -*- coding: utf-8 -*-
"""
Run STJ: Main module "glue" that connects Subtropical Jet Metric calc, plot and diags.

To run, set stj configuration file, start and end dates in `main()` and run with
`$ python run_stj.py`

Authors: Penelope Maher, Michael Kelleher

"""
import os
import sys
import pkg_resources
import multiprocessing
import logging
import argparse as arg
import datetime as dt
import warnings
import numpy as np
import yaml
import STJ_PV.stj_metric as stj_metric
import STJ_PV.input_data as inp

from dask.distributed import Client, LocalCluster

CFG_DIR = pkg_resources.resource_filename('STJ_PV', 'conf')


class JetFindRun:
    """
    Class containing properties about an individual attempt to find the subtropical jet.

    Parameters
    ----------
    config : string, optional
        Location of YAML-formatted configuration file, default None

    Attributes
    ----------
    data_source : string
        Path to input data configuration file
    config : dict
        Dictionary of properties of the run
    freq : Tuple
        Output data frequency (time, spatial)
    method : STJMetric
        Jet finder type
    log : :py:class:`logging.Logger`
        Debug log

    """

    def __init__(self, config_file=None):
        """Initialise jet finding attempt."""
        now = dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        if config_file is None:
            # Use default parameters if none are specified
            self.config = {'data_cfg': 'data_config_default.yml',
                           'freq': 'mon',
                           'method': 'STJPV', 'log_file': "stj_find_{}.log".format(now),
                           'zonal_opt': 'mean', 'poly': 'cheby',
                           'pv_value': 2.0, 'fit_deg': 6, 'min_lat': 10.0,
                           'max_lat': 65.0, 'update_pv': False,
                           'year_s': 1979, 'year_e': 2015}
        else:
            # Open the configuration file, put its contents into a variable to be read by
            # YAML reader
            self.config, cfg_failed = check_run_config(config_file)
            if cfg_failed:
                print('CONFIG CHECKS FAILED...EXITING')
                sys.exit(1)

            if '{}' in self.config['log_file']:
                # Log file name contains a format placeholder, use current time
                self.config['log_file'] = self.config['log_file'].format(now)

        # Format the data configuration file location with CFG_DIR
        _data_file = os.path.join(CFG_DIR, self.config['data_cfg'])
        self.data_cfg, data_cfg_failed = check_data_config(_data_file)

        if data_cfg_failed:
            print('DATA CONFIG CHECKS FAILED...EXITING')
            sys.exit(1)

        if self.data_cfg['single_var_file']:
            for var in ['uwnd', 'vwnd', 'tair', 'omega']:   # TODO: Need to change list
                if var not in self.data_cfg['file_paths']:
                    # This replicates the path in 'all' so each variable points to it
                    # this allows for the same loop no matter if data is in multiple files
                    self.data_cfg['file_paths'][var] = self.data_cfg['file_paths']['all']

        if 'wpath' not in self.data_cfg:
            # Sometimes, can't write to original data's path, so wpath is set
            # if it isn't, then wpath == path is fine, set that here
            self.data_cfg['wpath'] = self.data_cfg['path']

        # Pre-define all attributes
        self.th_levels = None
        self.p_levels = None
        self.metric = None

        self._set_metric()
        self.log_setup()

    def __str__(self):
        out_str = '{0} {1} {0}\n'.format('#' * 10, 'Run Config ')
        for param in self.config:
            out_str += '{:15s}: {}\n'.format(param, self.config[param])
        out_str += '{0} {1} {0}\n'.format('#' * 10, 'Data Config')

        for param in self.data_cfg:
            out_str += '{:15s}: {}\n'.format(param, self.data_cfg[param])
        return out_str

    def _set_metric(self):
        """Set metric and associated levels."""
        if self.config['method'] == 'STJPV':
            # self.th_levels = np.array([265.0, 275.0, 285.0, 300.0, 315.0, 320.0, 330.0,
            #                            350.0, 370.0, 395.0, 430.0])
            self.th_levels = np.arange(300.0, 430.0, 10).astype(np.float32)
            self.metric = stj_metric.STJPV
        elif self.config['method'] == 'STJUMax':
            self.p_levels = np.array([1000., 925., 850., 700., 600., 500., 400., 300.,
                                      250., 200., 150., 100., 70., 50., 30., 20., 10.])
            self.metric = stj_metric.STJMaxWind
        elif self.config['method'] == 'KangPolvani':
            self.metric = stj_metric.STJKangPolvani
        elif self.config['method'] == 'DavisBirner':
            self.metric = stj_metric.STJDavisBirner
        else:
            self.metric = None

    def _set_output(self, date_s=None, date_e=None):

        if self.config['method'] == 'STJPV':
            self.config['output_file'] = ('{short_name}_{method}_pv{pv_value}_'
                                          'fit{fit_deg}_y0{min_lat}_yN{max_lat}'
                                          .format(**dict(self.data_cfg, **self.config)))

            self.metric = stj_metric.STJPV

        elif self.config['method'] == 'STJUMax':
            self.config['output_file'] = ('{short_name}_{method}_pres{pres_level}'
                                          '_y0{min_lat}_yN{max_lat}'
                                          .format(**dict(self.data_cfg, **self.config)))

            self.metric = stj_metric.STJMaxWind

        elif self.config['method'] == 'KangPolvani':

            self.config['output_file'] = ('{short_name}_{method}'
                                          .format(**dict(self.data_cfg, **self.config)))
            self.metric = stj_metric.STJKangPolvani

        elif self.config['method'] == 'DavisBirner':

            self.config['output_file'] = ('{short_name}_{method}'
                                          .format(**dict(self.data_cfg, **self.config)))
            self.metric = stj_metric.STJDavisBirner

        else:

            self.config['output_file'] = ('{short_name}_{method}'
                                          .format(**dict(self.data_cfg, **self.config)))
            self.metric = None

        # Add the zonal option to the output name
        self.config['output_file'] += '_z{zonal_opt}'.format(**self.config)

        if 'lon_s' in self.data_cfg and 'lon_e' in self.data_cfg:
            self.config['output_file'] += ('_lon{lon_s:0d}-{lon_e:0d}'
                                           .format(**self.data_cfg))

        if date_s is not None and isinstance(date_s, dt.datetime):
            self.config['output_file'] += '_{}'.format(date_s.strftime('%Y-%m-%d'))

        if date_e is not None and isinstance(date_e, dt.datetime):
            self.config['output_file'] += '_{}'.format(date_e.strftime('%Y-%m-%d'))

    def log_setup(self):
        """Create a logger object with file location from `self.config`."""
        logger = logging.getLogger(self.config['method'])
        logger.setLevel(logging.DEBUG)

        log_file_handle = logging.FileHandler(self.config['log_file'])
        log_file_handle.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        log_file_handle.setFormatter(formatter)

        logger.addHandler(log_file_handle)
        self.log = logger

    def _get_data(self, date_s=None, date_e=None):
        """Retrieve data stored according to `self.data_cfg`."""
        if self.config['method'] == 'STJPV':
            data = inp.InputDataSTJPV(self, date_s, date_e)
        elif self.config['method'] in ['STJUMax', 'DavisBirner']:
            data = inp.InputDataUWind(self, date_s, date_e)
        else:
            data = inp.InputDataUWind(self, ['uwnd', 'vwnd'], date_s, date_e)

        return data.get_data()

    def run(self, date_s=None, date_e=None, save=True):
        """
        Find the jet, save location to a file.

        Parameters
        ----------
        date_s, date_e : :class:`datetime.datetime`
            Beginning and end dates, optional. If not included,
            use (Jan 1, self.year_s) and/or (Dec 31, self.year_e)

        """
        if date_s is None:
            date_s = dt.datetime(self.config['year_s'], 1, 1)
        if date_e is None:
            date_e = dt.datetime(self.config['year_e'], 12, 31)

        self._set_output(date_s, date_e)

        if self.data_cfg['single_year_file'] and date_s.year != date_e.year:
            for year in range(date_s.year, date_e.year + 1):
                _date_s = dt.datetime(year, 1, 1, 0, 0)
                _date_e = dt.datetime(year, 12, 31, 23, 59)
                self.log.info('FIND JET FOR %s - %s', _date_s.strftime('%Y-%m-%d'),
                              _date_e.strftime('%Y-%m-%d'))
                data = self._get_data(_date_s, _date_e)
                jet = self.metric(self, data)

                for shemis in [True, False]:
                    jet.find_jet(shemis)
                jet.compute()

                if year == date_s.year:
                    jet_all = jet
                else:
                    jet_all.append(jet)
                jet_all.save_jet()
        else:
            data = self._get_data(date_s, date_e)
            jet_all = self.metric(self, data)
            for shemis in [True, False]:
                jet_all.find_jet(shemis)

        if save:
            _out = None
            jet_all.save_jet()
        else:
            _out = jet_all

        return _out

    def run_sensitivity(self, sens_param, sens_range, date_s=None, date_e=None):
        """
        Perform a parameter sweep on a particular parameter of the JetFindRun.

        Parameters
        ----------
        sens_param : string
            Configuration parameter of :py:meth:`~STJ_PV.run_stj.JetFindRun`
        sens_range : iterable
            Range of values of `sens_param` over which to iterate
        date_s, date_e : :class:`datetime.datetime`, optional
            Start and end dates, respectively. Optional, defualts to config file defaults

        """
        params_avail = ['fit_deg', 'pv_value', 'min_lat', 'max_lat']
        if sens_param not in params_avail:
            print('SENSITIVITY FOR {} NOT AVAILABLE'.format(sens_param))
            print('POSSIBLE PARAMS:')
            for param in params_avail:
                print(param)
            sys.exit(1)
        for param_val in sens_range:
            # Fix the parameter type so it outputs using yaml.safe_dump when we call
            # STJMetric.save_jet(), this prevents a yaml.representer.RepresenterError
            # Because yaml.safe_dump can't interpret numpy floats or ints as of v5.1
            if isinstance(param_val, (np.float64, np.float32, np.float16)):
                param_val = float(param_val)
            elif isinstance(param_val, (np.int8, np.uint8, np.int16, np.int32, np.int64)):
                param_val = int(param_val)

            self.log.info('----- RUNNING WITH %s = %f -----', sens_param, param_val)
            # Save original config value
            param_orig = self.config[sens_param]

            self.config[sens_param] = param_val
            self._set_output(date_s, date_e)
            self.log.info('OUTPUT TO: %s', self.config['output_file'])
            self.run(date_s, date_e)

            # Reset to original config value
            self.config[sens_param] = param_orig


def check_config_req(cfg_file, required_keys_all, id_file=True):
    """
    Check that required keys exist within a configuration file.

    Parameters
    ----------
    cfg_file : string
        Path to configuration file
    required_keys_all : list
        Required keys that must exist in configuration file

    Returns
    -------
    config : dict
        Dictionary of loaded configuration file
    mkeys : bool
        True if required keys are missing

    """
    with open(cfg_file) as cfg:
        config = yaml.safe_load(cfg.read())

    if id_file:
        print('{0} {1:^40s} {0}'.format(7 * '#', cfg_file))
    keys_in = config.keys()
    missing = []
    wrong_type = []
    for key in required_keys_all:
        if key not in keys_in:
            missing.append(key)
            check_str = u'[\U0001F630  MISSING]'
        elif not isinstance(config[key], required_keys_all[key]):
            wrong_type.append(key)
            check_str = u'[\U0001F621  WRONG TYPE]'
        else:
            check_str = u'[\U0001F60E  OKAY]'
        print(u'{:30s} {:30s}'.format(key, check_str))

    # When either `missing` or `wrong_type` have values, this will evaluate `True`
    if missing or wrong_type:
        print(u'{} {:2d} {:^27s} {}'.format(12 * '>', len(missing) + len(wrong_type),
                                            'KEYS MISSING OR WRONG TYPE', 12 * '<'))

        for key in missing:
            print(u'    MISSING: {} TYPE: {}'.format(key, required_keys_all[key]))
        for key in wrong_type:
            print(u'    {} ({}) IS WRONG TYPE SHOULD BE {}'
                  .format(key, type(config[key]), required_keys_all[key]))
        mkeys = True
    else:
        mkeys = False

    return config, mkeys


def check_run_config(cfg_file):
    """
    Check the settings in a run configuration file.

    Parameters
    ----------
    cfg_file : string
        Path to configuration file

    """
    required_keys_all = {'data_cfg': str, 'freq': str, 'zonal_opt': str, 'method': str,
                         'log_file': str, 'year_s': int, 'year_e': int}

    config, missing_req = check_config_req(cfg_file, required_keys_all)

    # Optional checks
    missing_optionals = []
    if not missing_req:
        if config['method'] not in ['STJPV', 'STJUMax', 'KangPolvani']:
            # config must have pfac if it's pressure level data
            missing_optionals.append(False)
            print('NO METHOD FOR HANDLING: {}'.format(config['method']))

        elif config['method'] == 'STJPV':
            opt_keys = {'poly': str, 'fit_deg': int, 'pv_value': float,
                        'min_lat': float, 'max_lat': float}
            _, missing_opt = check_config_req(cfg_file, opt_keys, id_file=False)
            missing_optionals.append(missing_opt)

        elif config['method'] == 'STJUMax':
            opt_keys = {'pres_level': float, 'min_lat': float}
            _, missing_opt = check_config_req(cfg_file, opt_keys, id_file=False)
            missing_optionals.append(missing_opt)
        elif config['method'] == 'KangPolvani':
            opt_keys = {'pres_level': float}
            _, missing_opt = check_config_req(cfg_file, opt_keys, id_file=False)
            missing_optionals.append(missing_opt)

    return config, any([missing_req, all(missing_optionals)])


def check_data_config(cfg_file):
    """
    Check the settings in a data configuration file.

    Parameters
    ----------
    cfg_file : string
        Path to configuration file

    """
    required_keys_all = {'path': str, 'short_name': str, 'single_var_file': bool,
                         'single_year_file': bool, 'file_paths': dict, 'pfac': float,
                         'lon': str, 'lat': str, 'lev': str, 'time': str, 'ztype': str}

    config, missing_req = check_config_req(cfg_file, required_keys_all)
    # Optional checks
    missing_optionals = []
    if not missing_req:
        if config['ztype'] == 'pres':
            # config must have pfac if it's pressure level data
            opt_reqs = {'pfac': float}
            _, miss_opts = check_config_req(cfg_file, opt_reqs)
            missing_optionals.append(miss_opts)

        elif config['ztype'] not in ['pres', 'theta']:
            print('NO METHOD TO HANDLE {} level data'.format(config['ztype']))
            missing_optionals.append(True)
        else:
            missing_optionals.append(False)
    return config, any([missing_req, all(missing_optionals)])


def make_parse():
    """Make command line argument parser with argparse."""
    parser = arg.ArgumentParser(description='Find the sub-tropical jet')

    parser.add_argument('--sample', action='store_true',
                        help='Perform a sample run', default=False)

    parser.add_argument('--sens', action='store_true',
                        help='Perform a parameter sensitivity run',
                        default=False)

    parser.add_argument('--warn', action='store_true',
                        help='Show all warning messages', default=False)

    parser.add_argument('--file', type=str, default=None,
                        help='Configuration file path')
    parser.add_argument('--ys', type=str, default="1979", help="Start Year")
    parser.add_argument('--ye', type=str, default="2018", help="End Year")
    args = parser.parse_args()
    return args


def main(sample_run=True, sens_run=False, cfg_file=None, year_s=1979, year_e=2018):
    """Run the STJ Metric given a configuration file."""
    # Generate an STJProperties, allows easy access to these properties across methods.

    if sample_run:
        # ----------Sample test case-------------
        jf_run = JetFindRun('{}/stj_config_sample.yml'.format(CFG_DIR))
        date_s = dt.datetime(2016, 1, 1)
        date_e = dt.datetime(2016, 1, 3)

    elif cfg_file is not None:
        print(f'Run with {cfg_file}')
        jf_run = JetFindRun(cfg_file)

    else:
        # ----------Other cases-------------
        # jf_run = JetFindRun('{}/stj_kp_erai_daily.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_merra_daily.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_ncep_monthly.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_jra55_theta_mon.yml'.format(CFG_DIR))

        # Four main choices
        jf_run = JetFindRun('{}/stj_config_erai_theta.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_erai_theta_daily.yml'.format(CFG_DIR))
        # jf_run = JetFindRun(
        #     '{}/stj_config_erai_monthly_davisbirner_gv.yml'.format(CFG_DIR)
        # )

        # jf_run = JetFindRun('{}/stj_config_cfsr_mon.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_cfsr_day.yml'.format(CFG_DIR))

        # jf_run = JetFindRun('{}/stj_config_jra55_mon.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_jra55_day.yml'.format(CFG_DIR))

        # jf_run = JetFindRun('{}/stj_config_merra_monthly.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_merra_daily.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_jra55_monthly_cades.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_cfsr_monthly.yml'.format(CFG_DIR))
        # jf_run = JetFindRun('{}/stj_config_jra55_daily_titan.yml'.format(CFG_DIR))

        # ---------U-Max----------
        # jf_run = JetFindRun('{}/stj_umax_erai_pres.yml'.format(CFG_DIR))
        # ---Davis-Birner (2016)--
        # jf_run = JetFindRun('{}/stj_config_erai_monthly_davisbirner_gv.yml'
        #                     .format(CFG_DIR))

    if not sample_run:
        date_s = dt.datetime(year_s, 1, 1)
        date_e = dt.datetime(year_e, 12, 31)

    cpus = multiprocessing.cpu_count()
    if cpus % 4 == 0:
        _threads = 4
    elif cpus % 3 == 0:
        _threads = 3
    else:
        _threads = 2

    cluster = LocalCluster(n_workers=cpus // _threads, threads_per_worker=_threads)

    client = Client(cluster)
    jf_run.log.info(client)

    if sens_run:
        sens_param_vals = {'pv_value': np.arange(1.0, 4.5, 0.5),
                           'fit_deg': np.arange(3, 9),
                           'min_lat': np.arange(2.5, 15, 2.5),
                           'max_lat': np.arange(60., 95., 5.)}

        for sens_param in sens_param_vals:
            jf_run.run_sensitivity(sens_param=sens_param,
                                   sens_range=sens_param_vals[sens_param],
                                   date_s=date_s, date_e=date_e)
    else:
        jf_run.run(date_s, date_e)
    client.close()
    jf_run.log.info('JET FINDING COMPLETE')


if __name__ == "__main__":
    ARGS = make_parse()
    if not ARGS.warn:
        # Running with --warn will display all warnings, which includes the
        # warnings explicitly silenced below, otherwise, only warnings
        # that haven't been planned for show up
        np.seterr(all='ignore')
        # This will occur for some polynomial fits were only a few points are valid
        # which is dealt with in other ways
        warnings.simplefilter('ignore', np.polynomial.polyutils.RankWarning)

        # Ignore Runtime Warnings like:
        # ...dask/core.py:119: RuntimeWarning: invalid value encountered in greater
        # This occurs because not all points are valid, so dask/xarray warn, but this
        # is expected since isentropic and isobaric surfaces frequently go below ground
        warnings.simplefilter('ignore', RuntimeWarning)

    main(
        sample_run=ARGS.sample,
        sens_run=ARGS.sens,
        cfg_file=ARGS.file,
        year_s=int(ARGS.ys),
        year_e=int(ARGS.ye)
    )
