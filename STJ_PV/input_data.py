# -*- coding: utf-8 -*-
"""Generate or load input data for STJ Metric."""
import os
import numpy as np
import netCDF4 as nc
# Dependent code
import utils
import STJ_PV.data_out as dout
import psutil
import pkg_resources

__author__ = "Penelope Maher, Michael Kelleher"


def package_data(relpath, file_name):
    """Get data relative to this installed package.
    Generally used for the sample data."""
    _data_dir = pkg_resources.resource_filename('STJ_PV', relpath)
    return nc.Dataset(os.path.join(_data_dir, file_name), 'r')


class InputData(object):
    """
    Contains the relevant input data and routines for an JetFindRun.

    Parameters
    ----------
    jet_find : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Object containing properties about the metric calculation to be done. Used to
        locate correct files, and variables within those files.
    year : int, optional
        Year of data to load, not used when all years are in a single file

    """

    def __init__(self, props, date_s=None, date_e=None):
        """Initialize InputData object, using JetFindRun class."""
        self.props = props
        self.config = props.config
        self.data_cfg = props.data_cfg
        if date_s is not None:
            self.year = date_s.year
        else:
            self.year = None

        # Initialize attributes defined in open_files or open_ipv_data
        self.time = None
        self.time_units = None
        self.calendar = None

        self.lon = None
        self.lat = None
        self.lev = None
        self.th_lev = None

        # Each input data _must_ have u-wind, isentropic pv, and thermal tropopause,
        # but _might_ need the v-wind and air temperature to calculate PV/thermal-trop
        self.uwnd = None
        self.ipv = None
        self.dyn_trop = None
        self.in_data = None

        if date_s is not None or date_e is not None:
            self._select(date_s, date_e)
        else:
            self.d_select = slice(None)

    def _select(self, date_s=None, date_e=None):
        """
        Return a subset of the data between two times.

        Parameters
        ----------
        date_s, date_e : :py:meth:`datetime.datetime` for start and end of selection,
            optional. Default: None

        """
        if self.time is None:
            self._load_time(self._find_pv_update())
        dates = nc.num2date(self.time, self.time_units, self.calendar)
        if date_s is not None and date_e is not None:
            # We have both start and end
            self.d_select = np.logical_and(dates >= date_s, dates <= date_e)

        elif date_s is None and date_e is not None:
            # Beginning of data to an endpoint
            self.d_select = dates <= date_e

        elif date_s is not None and date_e is None:
            # Start time to end of data
            self.d_select = dates >= date_s

        self.time = self.time[self.d_select]

    def _find_pv_update(self):
        pv_file = os.path.join(self.data_cfg['wpath'],
                               self.data_cfg['file_paths']['ipv'].format(year=self.year))
        return self.config['update_pv'] or not os.path.exists(pv_file)

    def get_data_input(self):
        """Get input data for metric calculation."""
        # First, check if we want to update data, or need to create from scratch
        # if not, then we can load existing data

        pv_update = self._find_pv_update()
        if pv_update:
            self._load_data(pv_update)
            self._calc_ipv()
            if 'force_write' in self.props.config:
                force_write = self.props.config['force_write']
            else:
                force_write = False
            if self.time.shape[0] >= self.d_select.shape[0] or force_write:
                # Only write output if it's the entire file
                self._write_ipv()
            else:
                self.props.log.info('NOT WRITING FILE: time: {}, d_select: {}'
                                    .format(self.time.shape[0], self.d_select.shape[0]))
        else:
            self._load_ipv()

        if self.th_lev[0] > self.th_lev[-1]:
            self.ipv = self.ipv[:, ::-1, ...]
            self.uwnd = self.uwnd[:, ::-1, ...]
            self.th_lev = self.th_lev[::-1]

    def check_input_range(self, year_s, year_e):
        """
        Create/check input data for a range of years.

        Parameters
        ----------
        year_s, year_e : int
            Start and end years of period, respectively

        """
        cfg = self.data_cfg
        pv_file_fmt = os.path.join(cfg['wpath'], cfg['file_paths']['ipv'])

        for year in range(year_s, year_e + 1):
            self.year = year
            self.props.log.info('CHECKING INPUT FOR {}'.format(year))
            pv_file = pv_file_fmt.format(year=self.year)
            self.props.log.info('CHECKING: {}'.format(pv_file))

            pv_update = self._find_pv_update()

            if pv_update:
                self._load_data(pv_update)
                self._calc_ipv()
                self._write_ipv()

    def _load_time(self, pv_update=False):
        if pv_update:
            var = 'uwnd'
        else:
            var = 'ipv'

        # Load an example file
        try:
            file_name = self.data_cfg['file_paths'][var].format(year=self.year)
        except KeyError:
            file_name = self.data_cfg['file_paths']['all'].format(year=self.year)

        if not os.path.exists(os.path.join(self.data_cfg['path'], file_name)):
            # Fall back to 'all' if the input file is not found
            file_name = self.data_cfg['file_paths']['all'].format(year=self.year)

        try:
            nc_file = nc.Dataset(os.path.join(self.data_cfg['path'], file_name), 'r')
        except FileNotFoundError:
            nc_file = package_data(self.data_cfg['path'], file_name)

        self.time = nc_file.variables[self.data_cfg['time']][:]
        if isinstance(self.time, np.ma.MaskedArray):
            # Some data has masked time, if it is, remove those values
            # this issue may crop up elsewhere, but sort it there first
            self.time = self.time.compressed()

        # Set time units and calendar properties
        self.time_units = nc_file.variables[self.data_cfg['time']].units
        try:
            self.calendar = nc_file.variables[self.data_cfg['time']].calendar
        except (KeyError, AttributeError):
            self.calendar = 'standard'
        nc_file.close()

    def _load_data(self, pv_update=False):
        cfg = self.data_cfg
        self.in_data = {}

        data_vars = []
        if pv_update:
            data_vars.extend(['uwnd', 'vwnd', 'tair'])
            if 'epv' in cfg:
                # If we already have PV on isobaric levels, load only pv, u-wind, t-air
                data_vars = ['epv', 'uwnd', 'tair']

        if cfg['ztype'] == 'theta':
            # If input data is isentropic already..need pressure on theta, not air temp
            if pv_update:
                data_vars.remove('tair')
            data_vars.append('pres')

        # This is how they're called in the configuration file, each should point to
        # how the variable is called in the actual netCDF file
        dim_vars = ['lev', 'lat', 'lon']

        # Load u/v/t; create pv file that has ipv, tpause file with tropopause lev
        first_file = True
        nc_file = None
        for var in data_vars:
            nc_file, first_file = self._load_one_file(var, dim_vars,
                                                      nc_file, first_file)
            if cfg['single_var_file']:
                nc_file.close()
                nc_file = None

        if not cfg['single_var_file']:
            nc_file.close()

    def _load_one_file(self, var, dim_vars, nc_file=None, first_file=True):
        cfg = self.data_cfg
        vname = cfg[var]
        if self.in_data is None:
            self.in_data = {}

        if nc_file is None:
            # Format the name of the file, join it with the path, open it
            try:
                file_name = cfg['file_paths'][var].format(year=self.year)
            except KeyError:
                file_name = cfg['file_paths']['all'].format(year=self.year)
            self.props.log.info('OPEN: {}'.format(os.path.join(cfg['path'],
                                                               file_name)))
            try:
                nc_file = nc.Dataset(os.path.join(cfg['path'], file_name), 'r')
            except FileNotFoundError:
                nc_file = package_data(cfg['path'], file_name)

        # Load coordinate variables
        if first_file:
            if self.time is None:
                self._load_time()
            for dvar in dim_vars:
                v_in_name = cfg[dvar]
                if dvar == 'lev' and cfg['ztype'] == 'pres':
                    setattr(self, dvar,
                            nc_file.variables[v_in_name][:] * cfg['pfac'])
                else:
                    setattr(self, dvar, nc_file.variables[v_in_name][:])

        if 'lon_s' in cfg and 'lon_e' in cfg:

            # TODO: take account of longitudes being -180 - 180 or 0 - 360
            lon_sel = np.logical_and(self.lon >= cfg['lon_s'],
                                     self.lon <= cfg['lon_e'])
            if first_file:
                self.lon = self.lon[lon_sel]
        else:
            lon_sel = slice(None)

        if 'lev_s' in cfg and 'lev_e' in cfg:
            lev_sel = np.logical_and(self.lev >= cfg['lev_s'],
                                     self.lev <= cfg['lev_e'])
            if first_file:
                self.lev = self.lev[lev_sel]
        else:
            lev_sel = slice(None)

        first_file = False

        select = (self.d_select, lev_sel, slice(None), lon_sel)
        self.props.log.info("\tLOAD: {}".format(var))
        self.in_data[var] = nc_file.variables[vname][select].astype(np.float16)

        return nc_file, first_file


    def _gen_chunks(self, n_chunks=3):
        """Split data into time-period chunks if needed."""
        total_mem = psutil.virtual_memory().total
        # Data is in numpy float32, so total size is npoints * 32 / 8 in bytes
        dset_size = np.prod(self.in_data['uwnd'].shape) * 32 / 8
        ideal_chunks = int(np.floor(100 / (np.prod(self.in_data['uwnd'].shape) * 32 / 8 /
                                           psutil.virtual_memory().available * 100)))
        if ideal_chunks > n_chunks:
            n_chunks = ideal_chunks
        if (dset_size / total_mem) > 0.01:
            n_times = self.in_data['uwnd'].shape[0]
            # This sets the chunk width to at least 1
            cwidth = max(1, n_times // n_chunks)
            chunks = [[ix, ix + cwidth] for ix in range(0, n_times + cwidth, cwidth)]
            # Using the above range, the last chunk generated is beyond the shape of axis0
            chunks.pop(-1)
            # Set the last element of the last chunk to None, just in case, so all data
            # gets calculated no matter how the chunks are created
            chunks[-1][-1] = None
        else:
            chunks = [(0, None)]
        return chunks

    def _calc_ipv(self):
        # Shorthand for configuration dictionary
        cfg = self.data_cfg
        if self.in_data is None:
            self._load_data()
        self.props.log.info('Starting IPV calculation')

        # calculate IPV
        if cfg['ztype'] == 'pres':
            th_shape = list(self.in_data['uwnd'].shape)
            th_shape[1] = self.props.th_levels.shape[0]

            # Pre-allocate memory for PV and Wind fields
            self.ipv = np.ma.zeros(th_shape)
            self.uwnd = np.ma.zeros(th_shape)
            chunks = self._gen_chunks()
            self.props.log.info('CALCULATE IPV USING {} CHUNKS'.format(len(chunks)))
            for ix_s, ix_e in chunks:
                if 'epv' not in self.in_data:
                    self.props.log.info('USING U, V, T TO COMPUTE IPV')
                    self.ipv[ix_s:ix_e, ...], _, self.uwnd[ix_s:ix_e, ...] =\
                        utils.ipv(self.in_data['uwnd'][ix_s:ix_e, ...],
                                  self.in_data['vwnd'][ix_s:ix_e, ...],
                                  self.in_data['tair'][ix_s:ix_e, ...],
                                  self.lev, self.lat, self.lon, self.props.th_levels)
                else:
                    self.props.log.info('USING ISOBARIC PV TO COMPUTE IPV')
                    thta = utils.theta(self.in_data['tair'][ix_s:ix_e, ...], self.lev)
                    self.ipv[ix_s:ix_e, ...] = \
                        utils.vinterp(self.in_data['epv'][ix_s:ix_e, ...],
                                      thta, self.props.th_levels)
                    self.uwnd[ix_s:ix_e, ...] = \
                        utils.vinterp(self.in_data['uwnd'][ix_s:ix_e, ...],
                                      thta, self.props.th_levels)
            self.ipv *= 1e6  # Put PV in units of PVU
            self.th_lev = self.props.th_levels

        elif cfg['ztype'] == 'theta':
            self.ipv = utils.ipv_theta(self.in_data['uwnd'], self.in_data['vwnd'],
                                       self.in_data['pres'], self.lat, self.lon,
                                       self.lev)
        self.props.log.info('Finished calculating IPV')

    def _calc_dyn_trop(self):
        """Calculate dynamical tropopause (pv==2PVU)."""
        pv_lev = self.config['pv_value']
        pv_lev = np.array([abs(pv_lev)])

        self.props.log.info('Start calculating dynamical tropopause')
        if self.ipv is None:
            # Calculate PV
            self._load_ipv()

        # Calculate Theta on PV == 2 PVU
        _nh = [slice(None), slice(None), self.lat >= 0, slice(None)]
        _sh = [slice(None), slice(None), self.lat < 0, slice(None)]

        dyn_trop_nh = utils.vinterp(self.th_lev, self.ipv[_nh] * 1e6, pv_lev)
        dyn_trop_sh = utils.vinterp(self.th_lev, self.ipv[_sh] * 1e6, -1 * pv_lev)
        if self.lat[0] > self.lat[-1]:
            self.dyn_trop = np.append(dyn_trop_nh, dyn_trop_sh, axis=1)
        else:
            self.dyn_trop = np.append(dyn_trop_sh, dyn_trop_nh, axis=1)

        self.props.log.info('Finished calculating dynamical tropopause')

    def _write_ipv(self, out_file=None):
        """
        Save IPV data generated to a file, either netCDF4 or pickle.

        Parameters
        ----------
        out_file : string, optional
            Output file path for pickle or netCDF4 file, will contain ipv data and coords

        """
        if out_file is None:
            file_name = self.data_cfg['file_paths']['ipv'].format(year=self.year)
            out_file = os.path.join(self.data_cfg['wpath'], file_name)

        if not os.access(out_file, os.W_OK):
            write_dir = pkg_resources.resource_filename('STJ_PV', self.data_cfg['wpath'])
            out_file = os.path.join(write_dir, file_name)

        self.props.log.info('WRITE IPV: {}'.format(out_file))

        coord_names = ['time', 'lev', 'lat', 'lon']
        coords = {cname: getattr(self, cname) for cname in coord_names}
        coords['lev'] = self.th_lev

        props = {'name': 'isentropic_potential_vorticity',
                 'descr': 'Potential vorticity on theta levels',
                 'units': 'PVU', 'short_name': 'ipv', 'levvar': self.data_cfg['lev'],
                 'latvar': self.data_cfg['lat'], 'lonvar': self.data_cfg['lon'],
                 'timevar': self.data_cfg['time'], 'time_units': self.time_units,
                 'calendar': self.calendar, 'lat_units': 'degrees_north',
                 'lon_units': 'degrees_east', 'lev_units': 'K', '_FillValue': 9.0e16}

        # IPV in the file should be in 1e-6 PVU
        ipv_out = dout.NCOutVar(self.ipv * 1e-6, props=props, coords=coords)
        u_th_out = dout.NCOutVar(self.uwnd, props=dict(props), coords=coords)
        u_th_out.set_props({'name': 'zonal_wind_component',
                            'descr': 'Zonal wind on isentropic levels',
                            'units': 'm s-1', 'short_name': self.data_cfg['uwnd']})

        dout.write_to_netcdf([ipv_out, u_th_out], '{}'.format(out_file))
        self.props.log.info('Finished Writing')

    def _write_dyn_trop(self, out_file=None):
        """
        Save dynamical tropopause data generated to a file, either netCDF4 or pickle.

        Parameters
        ----------
        out_file : string, optional
            Output file path for pickle or netCDF4 file, will contain ipv data and coords

        """
        if out_file is None:
            file_name = self.data_cfg['file_paths']['dyn_trop'].format(year=self.year)
            out_file = os.path.join(self.data_cfg['wpath'], file_name)

        self.props.log.info('WRITE DYN TROP: {}'.format(out_file))

        coord_names = ['time', 'lat', 'lon']
        coords = {cname: getattr(self, cname) for cname in coord_names}
        props = {'name': 'dynamical_tropopause_theta',
                 'descr': 'Potential temperature on potential vorticity = 2PVU',
                 'units': 'K', 'short_name': 'dyntrop',
                 'latvar': self.data_cfg['lat'], 'lonvar': self.data_cfg['lon'],
                 'timevar': self.data_cfg['time'], 'time_units': self.time_units,
                 'calendar': self.calendar, 'lat_units': 'degrees_north',
                 'lon_units': 'degrees_east'}

        dyn_trop_out = dout.NCOutVar(self.dyn_trop, props=props, coords=coords)
        dout.write_to_netcdf([dyn_trop_out], '{}'.format(out_file))
        self.props.log.info('Finished Writing Dynamical Tropopause')

    def _load_ipv(self):
        """Open IPV file, load into self.ipv."""
        if self.time is None:
            self._load_time(pv_update=False)
        file_name = self.data_cfg['file_paths']['ipv'].format(year=self.year)
        in_file = os.path.join(self.data_cfg['wpath'], file_name)
        self.props.log.info("LOAD IPV FROM FILE: {}".format(in_file))
        try:
            ipv_in = nc.Dataset(in_file, 'r')
        except FileNotFoundError:
            ipv_in = package_data(self.data_cfg['wpath'], file_name)

        coord_names = ['lat', 'lon']
        for cname in coord_names:
            setattr(self, cname, ipv_in.variables[self.data_cfg[cname]][:])

        if 'lon_s' in self.data_cfg and 'lon_e' in self.data_cfg:
            # TODO: take account of longitudes being -180 - 180 or 0 - 360
            lon_sel = np.logical_and(self.lon >= self.data_cfg['lon_s'],
                                     self.lon <= self.data_cfg['lon_e'])
            self.lon = self.lon[lon_sel]
        else:
            lon_sel = slice(None)

        self.ipv = ipv_in.variables[self.data_cfg['ipv']][self.d_select, ..., lon_sel]
        self.ipv *= 1e6
        self._load_one_file('uwnd', ['lev', 'lat', 'lon'], first_file=False)
        self.uwnd = self.in_data['uwnd']
        # self.uwnd = ipv_in.variables[self.data_cfg['uwnd']][self.d_select, ..., lon_sel]

        self.th_lev = ipv_in.variables[self.data_cfg['lev']][:]
        ipv_in.close()


class InputDataWind(object):
    """
    Contains the relevant input data and routines for an JetFindRun.

    Parameters
    ----------
    jet_find : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Object containing properties about the metric calculation to be done. Used to
        locate correct files, and variables within those files.
    year : int, optional
        Year of data to load, not used when all years are in a single file

    """

    def __init__(self, props, var_names, date_s=None, date_e=None):
        """Initialize InputData object, using JetFindRun class."""
        self.props = props
        self.config = props.config
        self.data_cfg = props.data_cfg
        # if 'pres_level' in self.config:
        #     self.plev = self.config['pres_level']
        # else:
        #     self.plev = None
        self.plev = None

        if date_s is not None:
            self.year = date_s.year
        else:
            self.year = None

        # Initialize attributes defined in open_files or open_ipv_data
        self.time = None
        self.time_units = None
        self.calendar = None

        self.lon = None
        self.lat = None
        self.lev = None
        self.th_lev = None

        # Each input data _must_ have u-wind, isentropic pv, and thermal tropopause,
        # but _might_ need the v-wind and air temperature to calculate PV/thermal-trop
        self.in_data = None

        if isinstance(var_names, str):
            self.var_names = [var_names]
        else:
            self.var_names = var_names

        for var_name in self.var_names:
            setattr(self, var_name, None)

        self._load_time()

        if date_s is not None or date_e is not None:
            self._select(date_s, date_e)
        else:
            self.d_select = slice(None)

    def get_data_input(self):
        """Get input data for metric calculation."""
        # First, check if we want to update data, or need to create from scratch
        # if not, then we can load existing data
        cfg = self.data_cfg

        for var_name in self.var_names:

            self._load_data(var_name)
            if cfg['ztype'] == 'theta':
                if var_name == 'uwnd':
                    self._calc_interp(var_name)
            else:
                var_attrib = self.in_data[var_name]

            if len(self.lev) > 1 and (self.lev[0] > self.lev[-1]):
                var_attrib = self.in_data[var_name][:, ::-1, ...]
                self.lev = self.lev[::-1]

            setattr(self, var_name, var_attrib)

    def _select(self, date_s=None, date_e=None):
        """
        Return a subset of the data between two times.

        Parameters
        ----------
        date_s, date_e : :py:meth:`datetime.datetime` for start and end of selection,
            optional. Default: None

        """
        dates = nc.num2date(self.time, self.time_units, self.calendar)
        if date_s is not None and date_e is not None:
            # We have both start and end
            self.d_select = np.logical_and(dates >= date_s, dates <= date_e)

        elif date_s is None and date_e is not None:
            # Beginning of data to an endpoint
            self.d_select = dates <= date_e

        elif date_s is not None and date_e is None:
            # Start time to end of data
            self.d_select = dates >= date_s

        self.time = self.time[self.d_select]

    def _load_time(self):
        var = self.var_names[0]

        # Load an example file
        try:
            file_name = self.data_cfg['file_paths'][var].format(year=self.year)
        except KeyError:
            file_name = self.data_cfg['file_paths']['all'].format(year=self.year)

        try:
            nc_file = nc.Dataset(os.path.join(self.data_cfg['path'], file_name), 'r')
        except FileNotFoundError:
            nc_file = package_data(self.data_cfg['path'], file_name)


        self.time = nc_file.variables[self.data_cfg['time']][:]

        # Set time units and calendar properties
        self.time_units = nc_file.variables[self.data_cfg['time']].units
        try:
            self.calendar = nc_file.variables[self.data_cfg['time']].calendar
        except (KeyError, AttributeError):
            self.calendar = 'standard'
        nc_file.close()

    def _load_data(self, var_name):
        cfg = self.data_cfg
        self.in_data = {}

        data_vars = [var_name]

        if cfg['ztype'] == 'theta':
            # If input data is isentropic already..need pressure on theta, not air temp
            data_vars.append('pres')

        # This is how they're called in the configuration file, each should point to
        # how the variable is called in the actual netCDF file
        dim_vars = ['lev', 'lat', 'lon']

        # Load u/v/t; create pv file that has ipv, tpause file with tropopause lev
        first_file = True
        nc_file = None
        for var in data_vars:
            vname = cfg[var]
            if nc_file is None:
                # Format the name of the file, join it with the path, open it
                try:
                    file_name = cfg['file_paths'][var].format(year=self.year)
                except KeyError:
                    file_name = cfg['file_paths']['all'].format(year=self.year)

                self.props.log.info('OPEN: {}'.format(os.path.join(cfg['path'],
                                                                   file_name)))
                try:
                    nc_file = nc.Dataset(os.path.join(cfg['path'], file_name), 'r')
                except FileNotFoundError:
                    nc_file = package_data(self.data_cfg['path'], file_name)

            self.props.log.info("\tLOAD: {}".format(var))
            if first_file:
                for dvar in dim_vars:
                    v_in_name = cfg[dvar]
                    if dvar == 'time':
                        setattr(self, dvar, nc_file.variables[v_in_name][:])
                    elif dvar == 'lev' and cfg['ztype'] == 'pres':
                        setattr(self, dvar, nc_file.variables[v_in_name][:] * cfg['pfac'])
                    else:
                        setattr(self, dvar, nc_file.variables[v_in_name][:])

                # Set time units and calendar properties
                self.time_units = nc_file.variables[cfg['time']].units
                try:
                    self.calendar = nc_file.variables[cfg['time']].calendar
                except (KeyError, AttributeError):
                    self.calendar = 'standard'

                first_file = False

            if self.plev is not None:
                lev_sel = self.lev == self.plev
            else:
                lev_sel = slice(None)

            try:
                self.in_data[var] = (nc_file.variables[vname][self.d_select, lev_sel, ...]
                                     .astype(np.float16))
            except IndexError:
                self.in_data[var] = (nc_file.variables[vname][self.d_select, ...]
                                     .astype(np.float16))


            if cfg['single_var_file']:
                nc_file.close()
                nc_file = None

        if not cfg['single_var_file']:
            nc_file.close()

    def _calc_interp(self, var_name):
        self.lev = self.props.p_levels
        data_interp = utils.vinterp(self.in_data[var_name], self.in_data['pres'],
                                    self.lev)
        setattr(self, var_name, data_interp)
