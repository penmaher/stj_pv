# -*- coding: utf-8 -*-
"""STJ Metric: Calculate the position of the subtropical jet in both hemispheres."""
import numpy as np
import numpy.polynomial as poly
from scipy import signal as sig
import matplotlib.pyplot as plt

import utils
import data_out as dio
plt.style.use('ggplot')

class STJMetric(object):
    """Generic Class containing Sub Tropical Jet metric methods and attributes."""

    def __init__(self, name=None, data=None, props=None):
        """
        Initialize a subtropical jet metric.

        Parameters
        ----------
        name : string
            Name of this type of method
        data : InputData
            Object containing required input data
        props : JetFindRun
            Properties about the current jet finding attempt, including log file

        """
        self.name = name
        self.data = data
        self.props = props.config
        self.log = props.log
        self.jet_lat = None
        self.jet_theta = None
        self.jet_intens = None
        self.time = None
        self.hemis = None
        self.plot_idx = 0

    def save_jet(self):
        """Save jet position to file."""
        # Create output variables
        props_lat = {'name': 'jet_latitude', 'descr': 'Latitude of subtropical jet',
                     'units': 'degrees_north', 'short_name': 'lat_sh', 'timevar': 'time',
                     'calendar': self.data.calendar, 'time_units': self.data.time_units}
        coords = {'time': self.time}

        if self.props['zonal_opt'].lower() != 'mean':
            props_lat['lonvar'] = 'lon'
            props_lat['lon_units'] = 'degrees_east'
            coords['lon'] = self.data.lon

        props_th = dict(props_lat)
        props_th['name'] = 'jet_theta'
        props_th['descr'] = 'Theta level of subtropical jet'
        props_th['units'] = 'K'
        props_th['short_name'] = 'theta_sh'

        props_int = dict(props_lat)
        props_int['name'] = 'jet_intensity'
        props_int['descr'] = 'Intensity of subtropical jet'
        props_int['units'] = 'm s-1'
        props_int['short_name'] = 'intens_sh'

        self.log.info("CREATE OUTPUT VARIABLES")
        lat_sh_out = dio.NCOutVar(self.jet_lat[0, ...], coords=coords, props=props_lat)
        theta_sh_out = dio.NCOutVar(self.jet_theta[0, ...], coords=coords, props=props_th)
        intens_sh_out= dio.NCOutVar(self.jet_intens[0, ...], coords=coords,
                                    props=props_int)

        props_lat_nh = dict(props_lat)
        props_th_nh = dict(props_th)
        props_int_nh = dict(props_int)
        props_lat_nh['short_name'] = 'lat_nh'
        props_th_nh['short_name'] = 'theta_nh'
        props_int_nh['short_name'] = 'intens_nh'

        lat_nh_out = dio.NCOutVar(self.jet_lat[1, ...], coords=coords, props=props_lat_nh)
        theta_nh_out = dio.NCOutVar(self.jet_theta[1, ...], coords=coords,
                                    props=props_th_nh)
        intens_nh_out= dio.NCOutVar(self.jet_intens[1, ...], coords=coords,
                                    props=props_int_nh)

        self.log.info("WRITE TO {output_file}".format(**self.props))
        # Write jet & theta positions to file
        dio.write_to_netcdf([lat_sh_out, theta_sh_out, intens_sh_out,
                             lat_nh_out, theta_nh_out, intens_nh_out],
                            self.props['output_file'] + '.nc')

    def append(self, other):
        """Append another metric's latitude and theta positon to this one."""
        self.jet_lat = np.append(self.jet_lat, other.jet_lat, axis=1)
        self.jet_theta = np.append(self.jet_theta, other.jet_theta, axis=1)
        self.jet_intens = np.append(self.jet_intens, other.jet_intens, axis=1)
        self.time = np.append(self.time, other.time, axis=0)


class STJPV(STJMetric):
    """
    Subtropical jet position metric using dynamic tropopause on isentropic levels.

    Parameters
    ----------
    props : :py:meth:`~STJ_PV.run_stj.JetFindRun`
        Class containing properties about the current search for the STJ
    data : :py:meth:`~STJ_PV.input_data.InputData`
        Input data class containing a year (or more) of required data
    """

    def __init__(self, props, data):
        """Initialise Metric using PV Gradient Method."""
        name = 'PVGrad'
        super(STJPV, self).__init__(name=name, props=props, data=data)

        if np.max(np.abs(self.data.ipv)) < 1.0:
            self.data.ipv *= 1e6    # Put PV into units of PVU from 1e-6 PVU

        # Some config options should be properties for ease of access
        self.pv_lev = self.props['pv_value']
        self.fit_deg = self.props['fit_deg']
        self.min_lat = self.props['min_lat']

        if self.props['poly'].lower() in ['cheby', 'cby', 'cheb', 'chebyshev']:
            self.pfit = poly.chebyshev.chebfit
            self.pder = poly.chebyshev.chebder
            self.peval = poly.chebyshev.chebval

        elif self.props['poly'].lower() in ['leg', 'legen', 'legendre']:
            self.pfit = poly.legendre.legfit
            self.pder = poly.legendre.legder
            self.peval = poly.legendre.legval

        elif self.props['poly'].lower() in ['poly', 'polynomial']:
            self.pfit = poly.polynomial.polyfit
            self.pder = poly.polynomial.polyder
            self.peval = poly.polynomial.polyval

        # Initialise latitude & theta output arrays with correct shape
        dims = self.data.ipv.shape
        if self.props['zonal_opt'].lower() == 'mean':
            self.jet_lat = np.zeros([2, dims[0]])
            self.jet_theta = np.zeros([2, dims[0]])
            self.jet_intens = np.zeros([2, dims[0]])
        else:
            self.jet_lat = np.zeros([2, dims[0], dims[-1]])
            self.jet_theta = np.zeros([2, dims[0], dims[-1]])
            self.jet_intens = np.zeros([2, dims[0], dims[-1]])

        self.time = self.data.time[:]
        self.tix = None
        self.xix = None

    def _poly_deriv(self, lat, data, y_s=None, y_e=None, deriv=1):
        """
        Calculate the `deriv`^th derivative of a one-dimensional array w.r.t. latitude.

        Parameters
        ----------
        data : array_like
            1D array of data, same shape as `self.data.lat`
        y_s, y_e : integers, optional
            Start and end indices of subset, default is None
        deriv : integer, optional
            Number of derivatives of `data` to take

        Returns
        -------
        poly_der : array_like
            1D array of 1st derivative of data w.r.t. latitude between indices y_s and y_e

        """
        # Determine where data is valid...Intel's Lin Alg routines fail when trying to do
        # a least squares fit on array with np.nan, use where it's valid to do the fit
        valid = np.isfinite(data[y_s:y_e])
        poly_fit = self.pfit(lat[y_s:y_e][valid], data[y_s:y_e][valid], self.fit_deg)
        poly_der = self.peval(lat[y_s:y_e], self.pder(poly_fit, deriv))

        return poly_der, (poly_fit, lat[y_s:y_e][valid])

    def find_jet(self, shemis=True):
        """
        Find the subtropical jet using input parameters.

        Parameters
        ----------
        shemis : logical, optional
            If True, find jet position in Southern Hemisphere, if False, find N.H. jet

        """
        if shemis and self.pv_lev < 0 or not shemis and self.pv_lev > 0:
            pv_lev = np.array([self.pv_lev])
        else:
            pv_lev = -np.array([self.pv_lev])

        # Find axis
        lat_axis = self.data.ipv.shape.index(self.data.lat.shape[0])

        if self.data.ipv.shape.count(self.data.lat.shape[0]) > 1:
            # Log a message about which matching dimension used since this
            # could be time or lev or lon if ntimes, nlevs or nlons == nlats
            self.log.info('ASSUMING LAT DIM IS: {} ({})'.format(lat_axis,
                                                                self.data.ipv.shape))

        self.hemis = [slice(None)] * self.data.ipv.ndim

        if shemis:
            self.hemis[lat_axis] = self.data.lat < 0
            lat = self.data.lat[self.data.lat < 0]
            hidx = 0
            # Link `extrema` function to argrelmax for SH
            extrema = sig.argrelmax
        else:
            self.hemis[lat_axis] = self.data.lat > 0
            lat = self.data.lat[self.data.lat > 0]
            hidx = 1
            # Link `extrema` function to argrelmin for NH
            extrema = sig.argrelmin

        # Get theta on PV==pv_level
        theta_xpv = utils.vinterp(self.data.th_lev, self.data.ipv[self.hemis], pv_lev)
        uwnd_xpv = utils.vinterp(self.data.uwnd[self.hemis], self.data.ipv[self.hemis],
                                 pv_lev)
        ushear = self._get_max_shear(uwnd_xpv)

        dims = theta_xpv.shape

        self.log.info('COMPUTING JET POSITION FOR %d TIMES HEMIS: %d', dims[0], hidx)
        for tix in range(dims[0]):
            if tix % 50 == 0 and dims[0] > 50:
                self.log.info('COMPUTING JET POSITION FOR %d', tix)
            self.tix = tix
            jet_loc = np.zeros(dims[-1])
            for xix in range(dims[-1]):
                self.xix = xix
                jet_loc[xix] = self._find_single_jet(theta_xpv[tix, :, xix],
                                                     lat, ushear[tix, :, xix], extrema)
                if not self.props['zonal_opt'].lower() == 'mean':
                    self.jet_lat[hidx, tix, xix] = lat[jet_loc[xix]]
                    self.jet_theta[hidx, tix, xix] = theta_xpv[tix, jet_loc[xix], xix]
                    self.jet_intens[hidx, tix, xix] = uwnd_xpv[tix, jet_loc[xix], xix]

            if self.props['zonal_opt'].lower() == 'mean':

                jet_lat = np.ma.masked_where(jet_loc == 0, lat[jet_loc.astype(int)])
                self.jet_lat[hidx, tix] = np.ma.median(jet_lat)

                # First take the zonal median of Theta on dyn. tropopause
                jet_theta = np.nanmedian(theta_xpv[tix, :, :], axis=-1)
                # Mask wherever jet_loc is undefined, jet_loc is a func. of longitude here
                jet_theta = np.ma.masked_where(jet_loc == 0,
                                               jet_theta[jet_loc.astype(int)])
                # Then save the zonal median of this to the correct position in output
                self.jet_theta[hidx, tix] = np.ma.median(jet_theta)

                # Now do the same for jet intensity
                jet_intens = np.nanmedian(uwnd_xpv[tix, :, :], axis=-1)
                jet_intens = np.ma.masked_where(jet_loc == 0,
                                                jet_intens[jet_loc.astype(int)])
                self.jet_intens[hidx, tix] = np.ma.median(jet_intens)


    def _get_max_shear(self, uwnd_xpv):
        dec_levs = self.data.lev[0] > self.data.lev[-1]
        if (self.data.data_cfg['ztype'] == 'pres' and dec_levs or
            self.data.data_cfg['ztype'] == 'theta' and not dec_levs):
            sfc_ix = 0
        else:
            sfc_ix = -1

        uwnd_sfc = self.data.uwnd[self.hemis][:, sfc_ix, :, :]
        return uwnd_xpv - uwnd_sfc

    def _find_single_jet(self, theta_xpv, lat, ushear, extrema):
        """
        Find jet location for a 1D array of theta on latitude.

        Parameters
        ----------
        theta_xpv : array_like
            Theta on PV level as a function of latitude
        lat : array_like
            1D array of latitude same shape as theta_xpv and ttrop
        ushear : array_like
            1D array along latitude axis of maximum surface - troposphere u-wind shear

        Returns
        -------
        jet_loc : int
            Index of jet location on latitude axis

        """
        # Restrict interpolation domain to a "reasonable" subset using a minimum latitude
        y_s = np.abs(np.abs(lat) - self.props['min_lat']).argmin()
        y_e = None

        # If latitude is in decreasing order, switch start & end
        # This makes sure we're selecting the latitude nearest the equator
        if abs(lat[0]) > abs(lat[-1]):
            y_s, y_e = y_e, y_s
        # Find derivative of dynamical tropopause
        dtheta, theta_fit = self._poly_deriv(lat, theta_xpv, y_s=y_s, y_e=y_e)

        jet_loc_all = extrema(dtheta)[0].astype(int)
        if y_s is not None:
            # If beginning of array is cut off rather than end, add cut-off to adjust
            jet_loc_all += y_s
        select = self.select_jet(jet_loc_all, lat, ushear)

        if self.plot_idx <= 30:
            self._debug_plot(lat, theta_xpv, theta_fit, dtheta, jet_loc_all,
                             y_s, y_e, select)

        return select

    def select_jet(self, locs, lat, ushear):
        """
        Select correct jet latitude given list of possible jet locations.

        Parameters
        ----------
        locs : list
            List of indicies of jet locations
        lat : array_like
            1D array of hemispheric latitude
        ushear : array_like
            1D array along latitude axis of maximum surface - troposphere u-wind shear

        Returns
        -------
        jet_loc : int
            Index of the jet location. Between [`0`, `lat.shape[0] - 1`]

        Notes
        -----
        * If the list of locations is empty, return ``0`` as the location, this is
          interpreted by :py:meth:`~find_jet` as missing.

        * If the list of locations is exactly one return that location.

        * Otherwise use the location with maximum zonal wind shear between lowest
          provided level and the dynamical tropopause.

        """
        if len(locs) == 0:
            jet_loc = 0

        elif len(locs) == 1:
            # This essentially converts a list of length 1 to a single int
            jet_loc = locs[0]

        elif len(locs) > 1:

            ushear_max = np.argmax(ushear[locs])
            jet_loc = locs[ushear_max]

            if False: #np.abs(lat[jet_loc]) > 60:
                try:
                    if np.min(lat) > 0:
                        uwnd_plt = self.data.uwnd[self.tix, :, self.data.lat > 0, self.xix]
                    else:
                        uwnd_plt = self.data.uwnd[self.tix, :, self.data.lat < 0, self.xix]
                    plt.contourf(lat, self.data.lev, uwnd_plt.T,
                                 np.linspace(-40, 40, 14), cmap='RdBu_r', extend='both')
                    plt.gca().invert_yaxis()
                    ylims = plt.gca().get_ylim()
                    for loc in locs:
                        plt.plot([lat[loc]] * 2, ylims, '--')
                    plt.plot([lat[jet_loc]] * 2, ylims, '-', lw=3.)
                    plt.gca().set_yscale('log')
                    plt.savefig('plots/plt_uwnd_t{:03d}_x{:03d}_{:05d}.png'
                                .format(self.tix, self.xix, self.plot_idx))
                    self.plot_idx += 1
                except Exception as err:
                    print("TRIED TO PLOT, COULDN'T")
                    print(uwnd_plt.shape, lat.shape, self.data.lev.shape)
                    print(err)

                plt.clf()
                plt.close()

        return jet_loc

    def _debug_plot(self, lat, theta_xpv, theta_fit, dtheta,
                    jet_loc_all, y_s, y_e, select):
        if self.xix > 10 and self.xix < 30:
            if y_s is None:
                y_si = 0
            else:
                y_si = y_s

            poly_fit = self.peval(theta_fit[1], theta_fit[0])

            fig, axis = plt.subplots(1, 1)
            ax1 = axis.twinx()

            axis.plot(lat, theta_xpv, label='D. Trop.')
            axis.plot(theta_fit[1], poly_fit, label='TH(fit)')

            axis.plot(lat[jet_loc_all], theta_xpv[jet_loc_all - y_si], 'C3o', ms=2.)
            axis.plot(lat[select], theta_xpv[select - y_si], 'C4x', ms=3.)

            ax1.plot(lat[y_s:y_e], dtheta, 'C2', label='D(th)/d(lat)')

            axis.legend()
            plt.savefig('plots/plt_jet_{:05d}_t{:03d}_x{:03d}.png'.format(self.plot_idx,
                                                                    self.tix, self.xix))
            self.plot_idx += 1
            plt.close()
