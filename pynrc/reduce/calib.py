from __future__ import absolute_import, division, print_function, unicode_literals
import numpy as np
import matplotlib
import matplotlib.pyplot as plt

import logging
_log = logging.getLogger('pynrc')

# Import libraries
import numpy as np
import os, gzip, json
from copy import deepcopy
from scipy import ndimage

from astropy.io import fits
from astropy.modeling import models, fitting

# Multiprocessing
import multiprocessing as mp
import traceback

# Program bar
from tqdm.auto import trange, tqdm

import pynrc
from pynrc.maths import robust
from pynrc.nrc_utils import pad_or_cut_to_size, jl_poly_fit, jl_poly
from pynrc.nrc_utils import hist_indices
from pynrc.detops import create_detops
from pynrc.reduce.ref_pixels import reffix_hxrg, channel_smooth_savgol, channel_averaging


class nircam_dark(object):

    def __init__(self, scaid, datadir, outdir, DMS=False, same_scan_direction=False,
                 reverse_scan_direction=False):
        
        self.DMS = DMS

        self.scaid = scaid
        # Directory information
        self._create_dir_structure(datadir, outdir)

        # Get header information and create a NIRCam detector timing instance
        hdr = fits.getheader(self.allfiles[0])
        self.det = create_detops(hdr, DMS=DMS)
        self.det.same_scan_direction = same_scan_direction
        self.det.reverse_scan_direction = reverse_scan_direction

        # Create masks for ref pixels, active pixels, and channels
        self._create_pixel_masks()

        # Get temperature information
        self._grab_temperature_data()

        # Time array
        nz = self.det.multiaccum.ngroup
        self.time_arr = np.arange(1, nz+1) * self.det.time_group

        # Initialize superbias and superdark attributes
        self._super_bias = None
        self._super_bias_sig = None
        self._super_dark = None
        self._super_dark_sig = None
        self._super_dark_ramp = None
        self._super_dark_deconv = None
        self._super_bias_deconv = None
        self._dark_ramp_dict = None
        self._pixel_masks = None

        # IPC info
        self._kernel_ipc = None
        self._kernel_ppc = None
        self._kernel_ipc_sig = None
        self._kernel_ppc_sig = None

        # Noise info
        self._ktc_noise = None
        self._cds_act_dict = None
        self._cds_ref_dict = None
        self._eff_noise_dict = None
        self._pow_spec_dict = None

        # Reference pixel properties
        self._ref_pixel_dict = None

        # Column variations
        self._column_variations = None
        self._column_prob_bad   = None

    # Directory and files
    @property
    def datadir(self):
        return self.paths_dict['datadir']
    @property
    def outdir(self):
        return self.paths_dict['outdir']
    @property
    def allfiles(self):
        return self.paths_dict['allfiles']

    # Temperature information
    @property
    def temperature_dict(self):
        return self._temperature_dict

    # Ramp shapes and sizes
    @property
    def dark_shape(self):
        """Shape of dark ramps"""
        nx = self.det.xpix
        ny = self.det.ypix
        nz = self.det.multiaccum.ngroup
        return (nz,ny,nx)
    @property
    def nchan(self):
        """Number of output channels"""
        return self.det.nout
    @property
    def nchans(self):
        """Number of output channels"""
        return self.det.nout
    @property
    def chsize(self):
        """Width of output channel"""
        return self.det.chsize

    # Array masks
    @property
    def mask_ref(self):
        return self._mask_ref
    @property
    def mask_act(self):
        if self.mask_ref is None:
            return None
        else:
            return ~self.mask_ref
    @property
    def mask_channels(self):
        return self._mask_channels

    # Bias and dark slope information
    @property
    def super_bias(self):
        return self._super_bias
    @property
    def super_bias_deconv(self):
        return self._super_bias_deconv 
    @property
    def super_dark(self):
        return self._super_dark
    @property
    def super_dark_deconv(self):
        return self._super_dark_deconv
    @property
    def super_dark_ramp(self):
        return self._super_dark_ramp
    @property
    def dark_ramp_dict(self):
        return self._dark_ramp_dict

    # Column variations
    @property
    def ref_pixel_dict(self):
        return self._ref_pixel_dict

    # Column variations
    @property
    def column_variations(self):
        return self._column_variations
    @property
    def column_prob_bad(self):
        return self._column_prob_bad

    # IPC/PPC Kernel info
    @property
    def kernel_ipc(self):
        return self._kernel_ipc
    @property
    def ipc_alpha_frac(self):
        """Fractional IPC value (alpha)"""
        if self.kernel_ipc is None:
            return None
        else:
            return self.kernel_ipc[1,2]
    @property
    def kernel_ppc(self):
        return self._kernel_ppc
    @property
    def ppc_frac(self):
        """Fractional PPC value"""
        if self.kernel_ppc is None:
            return None
        else:
            return self.kernel_ppc[1,2]

    @property
    def ktc_noise(self):
        return self._ktc_noise
    @property
    def cds_act_dict(self):
        return self._cds_act_dict
    @property
    def cds_ref_dict(self):
        return self._cds_ref_dict
    @property
    def eff_noise_dict(self):
        return self._eff_noise_dict
    @property
    def pow_spec_dict(self):
        return self._pow_spec_dict

    def _create_dir_structure(self, datadir, outdir):
        """ Directories and files"""

        scaid = self.scaid
        # Directory information
        indir = os.path.join(datadir, str(scaid)) + '/'
        # Get file names within directory
        allfits = [file for file in os.listdir(indir) if file.endswith('.fits')]
        allfits = np.sort(allfits)
        # Add directory 
        allfiles = [indir + f for f in allfits]

        # Directory to save figures for analysis
        figdir = os.path.join(outdir, str(scaid)) + '/'        

        # Directories to save super bias and super dark info
        super_bias_dir = os.path.join(outdir, 'SUPER_BIAS') + '/'
        super_dark_dir = os.path.join(outdir, 'SUPER_DARK') + '/'
        power_spec_dir = os.path.join(outdir, 'POWER_SPEC') + '/'

        # Make sure directories exist for writing
        for path in [outdir, figdir, super_bias_dir, super_dark_dir, power_spec_dir]:
            if not os.path.exists(path):
                os.mkdir(path)

        self.paths_dict = {
            'datadir ' : datadir,
            'allfiles' : allfiles,
            'outdir'   : outdir,
            'figdir'   : figdir,
            'super_bias_dir'      : super_bias_dir,
            'super_dark_dir'      : super_dark_dir,
            'super_bias_init'     : super_bias_dir + f'SUPER_BIAS_INIT_{scaid}.FITS',
            'super_bias'          : super_bias_dir + f'SUPER_BIAS_{scaid}.FITS',
            'super_dark_ramp'     : super_dark_dir + f'SUPER_DARK_RAMP_{scaid}.FITS',
            'super_dark'          : super_dark_dir + f'SUPER_DARK_{scaid}.FITS',
            'kernel_ipc'          : super_dark_dir + f'KERNEL_IPC_{scaid}.FITS',
            'kernel_ppc'          : super_dark_dir + f'KERNEL_PPC_{scaid}.FITS',
            'pixel_masks'         : super_dark_dir + f'PIXEL_MASKS_{scaid}.FITS.gz',
            'column_variations'   : super_dark_dir + f'SUPER_DARK_COLVAR_{scaid}.FITS',
            'ref_pix_variations'  : super_bias_dir + f'BIAS_BEHAVIOR_{scaid}.JSON',
            'cds_act_dict'        : super_dark_dir + f'CDS_NOISE_ACTIVE_{scaid}.JSON',
            'cds_ref_dict'        : super_dark_dir + f'CDS_NOISE_REF_{scaid}.JSON',
            'eff_noise_dict'      : super_dark_dir + f'EFF_NOISE_{scaid}.JSON',
            'power_spec_cds'      : power_spec_dir + f'POWER_SPEC_CDS_{scaid}.npy',
            'power_spec_full'     : power_spec_dir + f'POWER_SPEC_FULL_{scaid}.npy',
            'power_spec_cds_oh'   : power_spec_dir + f'POWER_SPEC_CDS_OH_{scaid}.npy',
            'power_spec_full_oh'  : power_spec_dir + f'POWER_SPEC_FULL_OH_{scaid}.npy',
            'power_spec_cds_pix'  : power_spec_dir + f'POWER_SPEC_CDS_PIX_{scaid}.npy',
            'power_spec_full_pix' : power_spec_dir + f'POWER_SPEC_FULL_PIX_{scaid}.npy',
        }

    def _create_pixel_masks(self):

        # Array masks
        ny, nx = self.dark_shape[-2:]
        nchan = self.nchan
        chsize = self.chsize

        lower, upper, left, right = self.det.ref_info

        ref_mask = np.zeros([ny,nx], dtype='bool')
        ref_mask[0:lower,:] = True
        ref_mask[-upper:,:] = True
        ref_mask[:,0:left] = True
        ref_mask[:,-right:] = True
        self._mask_ref = ref_mask

        ch_mask = np.zeros([ny,nx])
        for ch in np.arange(nchan):
            ch_mask[:,ch*chsize:(ch+1)*chsize] = ch
        self._mask_channels = ch_mask

    def _grab_temperature_data(self):
        """ Grab temperature data from headers
        
        Creates a dictionary that houses the temperature
        info stored in the headers of each FITS file.
        """

        # TODO: Add DMS support for temperature
        if self.DMS:
            self._temperature_dict = None
            _log.error("DMS data not yet supported obtaining array temperatures")
            return
            # raise NotImplementedError("DMS data not yet supported")
        
        # Get intial temperature keys
        hdr = fits.getheader(self.allfiles[0])
        # hdul = fits.open(self.allfiles[0])
        # hdr = hdul[0].header
        # hdul.close()

        tkeys = [k for k in list(hdr.keys()) if k[0:2]=='T_'] + ['ASICTEMP']

        # Initialize lists for each temperature key
        temperature_dict = {}
        for k in tkeys:
            temperature_dict[k] = []
            
        for f in self.allfiles:
            hdul = fits.open(f)
            hdr = hdul[0].header
            for k in tkeys:
                temperature_dict[k].append(float(hdr[k]))
            hdul.close()

        self._temperature_dict = temperature_dict

    def get_super_bias_init(self, deg=1, nsplit=2, force=False, **kwargs):

        _log.info("Generating initial super bias")

        allfiles = self.allfiles

        savename = self.paths_dict['super_bias_init']
        file_exists = os.path.isfile(savename)

        if file_exists and (not force):
            super_bias, super_bias_sig = get_fits_data(savename)
        else:
            
            # Default ref pixel correction kw args
            kwargs_def = {
                'nchans': self.nchan, 'altcol': True, 'in_place': True,    
                'fixcol': True, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
            }
            for k in kwargs_def.keys():
                if k not in kwargs:
                    kwargs[k] = kwargs_def[k]

            res = gen_super_bias(allfiles, deg=deg, nsplit=nsplit, DMS=self.DMS,
                                 return_std=True, **kwargs)
            super_bias, super_bias_sig = res

            # Save superbias frame to directory
            hdu = fits.PrimaryHDU(np.array([super_bias, super_bias_sig]))
            hdu.writeto(savename, overwrite=True)

        self._super_bias = super_bias
        self._super_bias_sig = super_bias_sig

    def get_super_bias_update(self, force=False, **kwargs):
        # Make sure initial super bias exists
        if (self._super_bias is None) or (self._super_bias_sig is None):
            self.get_super_bias_init(**kwargs)

        # File names
        fname = self.paths_dict['super_bias']
        file_exists = os.path.isfile(fname)
        if file_exists and (not force):
            # Grab updated Super Bias
            _log.info("Opening updated super bias")
            self._super_bias = get_fits_data(fname)
        else:
            # Generate Super Bias along with dark ramp and pixel masks
            self.get_super_dark_ramp(force=force, **kwargs)

    def get_super_dark_ramp(self, force=False, **kwargs):
        """Create or read super dark ramp"""

        # Make sure initial super bias exists
        if (self._super_bias is None) or (self._super_bias_sig is None):
            self.get_super_bias_init(**kwargs)

        _log.info("Creating super dark ramp cube, updated super bias, and pixel mask info")

        # File names
        fname_super_dark_ramp = self.paths_dict['super_dark_ramp']
        fname_super_bias      = self.paths_dict['super_bias']
        fname_pixel_mask      = self.paths_dict['pixel_masks']

        file_exists = os.path.isfile(fname_super_dark_ramp)
        if file_exists and (not force):

            # Grab Super Dark Ramp
            super_dark_ramp = get_fits_data(fname_super_dark_ramp)
            
            # Grab updated Super Bias
            super_bias = get_fits_data(fname_super_bias)
            
            # Generate pixel masks dictionary
            masks_dict = {}
            hdul = fits.open(fname_pixel_mask)
            for hdu in hdul:
                key = hdu.name.lower()
                masks_dict[key] = hdu.data.astype('bool')
            hdul.close()
        else:
            allfiles = self.allfiles

            # Default kwargs to run
            kwargs_def = {
                'nchans': self.nchan, 'altcol': True, 'in_place': True,    
                'fixcol': True, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
            }
            for k in kwargs_def.keys():
                if k not in kwargs:
                    kwargs[k] = kwargs_def[k]

            res = gen_super_dark(allfiles, super_bias=self.super_bias, DMS=self.DMS, **kwargs)
            super_dark_ramp, bias_off, masks_dict = res

            # Add residual bias offset
            super_bias += bias_off
            
            # Save updated superbias frame to directory
            hdu = fits.PrimaryHDU(super_bias)
            hdu.writeto(fname_super_bias, overwrite=True)
            
            # Save super dark ramp
            hdu = fits.PrimaryHDU(super_dark_ramp.astype(np.float32))
            # hdu = fits.PrimaryHDU(super_dark_ramp)
            hdu.writeto(fname_super_dark_ramp, overwrite=True)
            
            # Save mask dictionary to a compressed FITS file
            hdul = fits.HDUList()

            for k in masks_dict.keys():
                data = masks_dict[k].astype('uint8')
                hdu = fits.ImageHDU(data, name=k)
                hdul.append(hdu)

            output = gzip.open(fname_pixel_mask, 'wb')
            hdul.writeto(output, overwrite=True) 
            output.close()

        # Save as class attributes
        self._super_dark_ramp = super_dark_ramp
        self._super_bias = super_bias
        self._pixel_masks = masks_dict

    def get_dark_slope_image(self, deg=1, force=False):
        """ Calculate dark slope image"""

        _log.info('Calculating dark slope image...')
        fname = self.paths_dict['super_dark']

        file_exists = os.path.isfile(fname)
        if file_exists and (not force):
            # Grab Super Dark
            super_dark = get_fits_data(fname)
        else:
            if self._super_dark_ramp is None:
                self.get_super_dark_ramp()
            # Get dark slope image
            cf = jl_poly_fit(self.time_arr, self.super_dark_ramp, deg=deg)
            super_dark = cf[1]

            # Save super dark frame to directory
            hdu = fits.PrimaryHDU(super_dark)
            hdu.writeto(fname, overwrite=True)

        self._super_dark = super_dark

    def get_pixel_slope_averages(self, deg=1):
        """Get average pixel ramp"""

        if self._super_dark_ramp is None:
            _log.error("`super_dark_ramp` is not defined. Please run get_super_dark_ramp().")
            return
            # self.get_super_dark_ramp()

        _log.info('Calculating average pixel ramps...')

        nz = self.dark_shape[0]
        nchan = self.nchan
        chsize = self.chsize

        # Average slope in each channel
        ramp_avg_ch = []
        for ch in range(nchan):
            ramp_ch = self.super_dark_ramp[:,:,ch*chsize:(ch+1)*chsize]
            avg = np.median(ramp_ch.reshape([nz,-1]), axis=1)
            ramp_avg_ch.append(avg)
        ramp_avg_ch = np.array(ramp_avg_ch)

        # Average ramp for all pixels
        ramp_avg_all = np.mean(ramp_avg_ch, axis=0)

        self._dark_ramp_dict = {
            'ramp_avg_ch' : ramp_avg_ch,
            'ramp_avg_all' : ramp_avg_all
        }

    def get_ipc(self, calc_ppc=False):
        """Calculate IPC (and PPC) kernels"""
        
        if calc_ppc:
            _log.info("Calculating IPC and PPC kernels...")
        else:
            _log.info("Calculating IPC kernels...")

        fname_ipc = self.paths_dict['kernel_ipc']
        fname_ppc = self.paths_dict['kernel_ppc']

        gen_vals = False
        if os.path.isfile(fname_ipc):
            k_ipc, k_ipc_sig = get_fits_data(fname_ipc)
        else:
            gen_vals = True

        if calc_ppc:
            if os.path.isfile(fname_ppc):
                k_ppc, k_ppc_sig = get_fits_data(fname_ppc)
            else:
                gen_vals = True
        
        # Do we need to generate IPC/PPC values?
        if gen_vals:
            if self.super_dark_ramp is None:
                _log.error("`super_dark_ramp` is not defined. Please run get_super_dark_ramp().")
                return

            dark_ramp = self.super_dark_ramp[1:] - self.super_dark_ramp[0]

            # Subtract away averaged spatial background from each frame
            dark_med = ndimage.median_filter(self.super_dark, 7)
            tarr = self.time_arr[1:] - self.time_arr[0]
            for i, im in enumerate(dark_ramp):
                im -= dark_med*tarr[i]
                
            nchan = self.nchan
            chsize = self.chsize

            ssd = self.det.same_scan_direction
            rsd = self.det.reverse_scan_direction

            # Set the average of each channel in each image to 0
            for ch in np.arange(nchan):
                x1 = int(ch*chsize)
                x2 = int(x1 + chsize)

                dark_ramp_ch = dark_ramp[:,:,x1:x2]
                dark_ramp_ch = dark_ramp_ch.reshape([dark_ramp.shape[0],-1])
                chmed_arr = np.median(dark_ramp_ch, axis=1)
                dark_ramp[:,:,x1:x2] -= chmed_arr.reshape([-1,1,1])

            k_ipc_arr = []
            k_ppc_arr = []
            for im in dark_ramp[::4]:
                diff = dark_ramp[-1] - im
                res = get_ipc_kernel(diff, bg_remove=False, boxsize=5, calc_ppc=calc_ppc,
                                    same_scan_direction=ssd, reverse_scan_direction=rsd,
                                    suppress_error_msg=True)
                if res is not None:
                    if calc_ppc:
                        k_ipc, k_ppc = res
                        k_ppc_arr.append(k_ppc)
                    else:
                        k_ipc = res
                    k_ipc_arr.append(k_ipc)
                
            # Average IPC values
            k_ipc_arr = np.array(k_ipc_arr)
            k_ipc = robust.mean(k_ipc_arr, axis=0)
            k_ipc_sig = robust.std(k_ipc_arr, axis=0)
                
            # Ensure kernels are normalized to 1
            ipc_norm = k_ipc.sum()
            k_ipc /= ipc_norm
            k_ipc_sig /= ipc_norm

            # Save IPC kernel to file
            hdu = fits.PrimaryHDU(np.array([k_ipc, k_ipc_sig]))
            hdu.writeto(fname_ipc, overwrite=True)

            # PPC values
            if calc_ppc:
                k_ppc_arr = np.array(k_ppc_arr)
                k_ppc = robust.mean(k_ppc_arr, axis=0)
                k_ppc_sig = np.std(k_ppc_arr, axis=0)
                ppc_norm = k_ppc.sum()
                k_ppc /= ppc_norm
                k_ppc_sig /= ppc_norm

                # Save IPC kernel to file
                hdu = fits.PrimaryHDU(np.array([k_ppc, k_ppc_sig]))
                hdu.writeto(fname_ppc, overwrite=True)

        # Store kernel information
        self._kernel_ipc = k_ipc
        self._kernel_ipc_sig = k_ipc_sig
        
        alpha = k_ipc[1,2]
        alpha_sig = k_ipc_sig[1,2]
        _log.info('  IPC = {:.3f}% +/- {:.3f}%'.format(alpha*100, alpha_sig*100))

        # PPC values
        if calc_ppc:
            self._kernel_ppc = k_ppc
            self._kernel_ppc_sig = k_ppc_sig

            ppc = k_ppc[1,2]
            ppc_sig = k_ppc_sig[1,2]
            _log.info('  PPC = {:.3f}% +/- {:.3f}%'.format(ppc*100, ppc_sig*100))

    def get_ktc_noise(self, **kwargs):
        """Calculate and store kTC (Reset) Noise
        
        Keyword Args
        ------------
        bias_sigma_arr : ndarray
            Image of the pixel uncertainties.
        binsize : float
            Size of the histogram bins.
        return_std : bool
            Also return the standard deviation of the 
            distribution?

        """

        if self._super_bias_sig is None:
            # Make sure super bias sigma exists
            _log.info('Obtaining sigma image for super bias...')
            self.get_super_bias_init()

        _log.info("Calculating kTC Noise for active and reference pixels...")

        # kTC Noise (DN)
        im = self._super_bias_sig[self.mask_act]
        self._ktc_noise = calc_ktc(im, **kwargs)
        # kTC Noise of reference pixels
        im = self._super_bias_sig[self.mask_ref]
        self._ktc_noise_ref= calc_ktc(im, binsize=1)

    def get_cds_dict(self, force=False):
        """Calculate CDS noise for all files
        
        Creates a dictionary of CDS noise components, including 
        total noise, amplifier 1/f noise, correlated 1/f noise, 
        white noise, and reference pixel ratios. Two different
        methods are used to calculate CDS per pixels:
        temporal and spatial.

        Creates dictionary attributes `self.cds_act_dict`
        and `self.cds_ref_dict`.
        """

        _log.info("Building CDS Noise dictionaries...")

        ssd = self.det.same_scan_direction

        outname1 = self.paths_dict['cds_act_dict']
        outname2 = self.paths_dict['cds_ref_dict']
        both_exist = os.path.exists(outname1) and os.path.exists(outname2)
        if both_exist and (not force):

            # Load from JSON files
            with open(outname1, 'r') as fp:
                cds_act_dict = json.load(fp)
            with open(outname2, 'r') as fp:
                cds_ref_dict = json.load(fp)

        else:
            # Create CDS dictionaries
            cds_act_dict, cds_ref_dict = gen_cds_dict(
                self.allfiles, superbias=self.super_bias,
                mask_good_arr=self._pixel_masks['mask_poly'],
                same_scan_direction=ssd, DMS=self.DMS)

            # Save active pixel dictionary
            dtemp = deepcopy(cds_act_dict)
            for k in dtemp.keys():
                if isinstance(dtemp[k], (np.ndarray)):
                    dtemp[k] = dtemp[k].tolist()
            with open(outname1, 'w') as fp:
                json.dump(dtemp, fp, sort_keys=False, indent=4)

            # Save reference pixel dictionary
            dtemp = deepcopy(cds_ref_dict)
            for k in dtemp.keys():
                if isinstance(dtemp[k], (np.ndarray)):
                    dtemp[k] = dtemp[k].tolist()
            with open(outname2, 'w') as fp:
                json.dump(dtemp, fp, sort_keys=False, indent=4)

        # Convert any lists to np.array
        dlist = [cds_act_dict, cds_ref_dict]
        for d in dlist:
            for k in d.keys():
                if isinstance(d[k], (list)):
                    d[k] = np.array(d[k])

        self._cds_act_dict = cds_act_dict
        self._cds_ref_dict = cds_ref_dict

    def get_effective_noise(self, ideal_Poisson=False, force=False):
        "Calculate effective noise curves for each readout pattern"
        
        outname = self.paths_dict['eff_noise_dict']

        allfiles = self.allfiles
        superbias = self.super_bias

        det = self.det

        nchan = det.nout
        gain = det.gain

        patterns = list(det.multiaccum._pattern_settings.keys())

        if os.path.exists(outname) and (not force):
            # Load from JSON files
            with open(outname, 'r') as fp:
                dtemp = json.load(fp)

            # Convert to arrays
            for k in dtemp.keys():
                d2 = dtemp[k]
                out_list = [np.array(d2[patt]) for patt in patterns]
                dtemp[k] = out_list

            ng_all_list  = dtemp['ng_all_list']
            en_spat_list = dtemp['en_spat_list']

        else:
            ng_all_list = []
            en_spat_list = []
            #en_temp_list = []
            for patt in tqdm(patterns, leave=False):
                res = calc_eff_noise(allfiles, superbias=superbias, read_pattern=patt, temporal=False)
                # ng_all, eff_noise_temp, eff_noise_spa = res
                ng_all, eff_noise_spat = res
                
                # List of ngroups arrays
                ng_all_list.append(ng_all)
                en_spat_list.append(eff_noise_spat)
                #en_temp_list.append(eff_noise_temp)

            # Place variables into dictionary for saving to disk
            dtemp = {'ng_all_list' : ng_all_list, 'en_spat_list' : en_spat_list}

            # Make sure everything are in list format
            for k in dtemp.keys():
                arr = dtemp[k]
                d2 = {}
                for i, patt in enumerate(patterns):
                    d2[patt] = arr[i].tolist()
                dtemp[k] = d2

            # Save to a JSON file
            with open(outname, 'w') as fp:
                json.dump(dtemp, fp, sort_keys=False, indent=4)
            
        # tvals_all = []
        tarr_all = []
        for i, patt in enumerate(patterns):
            det_new = deepcopy(det)
            ma_new = det_new.multiaccum
            ma_new.read_mode = patt
            # ma_new.ngroup = int((det.multiaccum.ngroup - ma_new.nd1 + ma_new.nd2) / (ma_new.nf + ma_new.nd2))
            # tvals_all.append(det_new.times_group_avg)
            # Times associated with each calcualted group
            ng_all = ng_all_list[i]
            tarr_all.append((ng_all-1)*det_new.time_group)

        # Determine excess variance parameters
        from scipy.optimize import least_squares#, leastsq

        en_dn_list = []
        for i in range(len(patterns)):
            # Average spatial and temporal values
        #     var_avg_ch = (en_spat_list[i]**2 + en_temp_list[i]**2) / 2
        #     var_avg_ch = en_temp_list[i]**2
            var_avg_ch = en_spat_list[i]**2
            en_dn_list.append(np.sqrt(var_avg_ch[0:nchan].mean(axis=0)))

        # Average dark current (e-/sec)
        if self.dark_ramp_dict is None:
            idark_avg = det.dark_current
        else:
            idark = []
            tarr = self.time_arr
            for ch in np.arange(nchan):
                y = self.dark_ramp_dict['ramp_avg_ch'][ch]
                cf = jl_poly_fit(tarr, y, deg=1)
                idark.append(cf[1])
            idark = np.array(idark) * gain
            idark_avg = np.mean(idark)
            
        # Average read noise per frame (e-)
        cds_var = (en_dn_list[0][0] * det.time_group * gain)**2 - (idark_avg * det.time_group)
        read_noise = np.sqrt(cds_var / 2)

        p0 = [1.5,10]
        args=(det, patterns, ng_all_list, en_dn_list)
        kwargs = {'idark':idark_avg, 'read_noise':read_noise, 'ideal_Poisson':ideal_Poisson}
        res_lsq = least_squares(fit_func_var_ex, p0, args=args, kwargs=kwargs)
        p_excess = res_lsq.x
        _log.info("  Best fit excess variance model parameters: {}".format(p_excess))

        self._eff_noise_dict = {
            'patterns'      : patterns,     # Readout patterns
            'ng_all_list'   : ng_all_list,  # List of groups fit
            'tarr_all_list' : tarr_all,     # Associated time values
            'en_spat_list'  : en_spat_list, # Effective noise per channel (spatial)
            'p_excess'      : p_excess      # Excess variance model parameters (best fit)
        }


    def calc_cds_noise(self, cds_type='spatial', temperature=None, temp_key='T_FPA1'):
        """ Return CDS Noise components for each channel
        
        Parameters
        ----------
        cds_type : str
            Return 'spatial', 'temporal', or 'average' noise values?
        temperature : float or None
            Option to supply temperature at which to interpolate. If None is
            provided, then returns the median of all noise values.
        temp_key : str
            Temperature key from `self.temperature_dict` to interpolate over.
            Generally, either 'T_FPA1' or 'T_FPA2' as those most closely
            represent the detector operating temperatures.
        """

        def cds_fit(tval, temps, cds_per_ch):
            """Fit """
            cds_arr = []
            for ch in np.arange(self.nchan):
                cf = jl_poly_fit(temp_arr, cds_per_ch[:,ch])
                cds_arr.append(jl_poly(temperature, cf))
            return np.array(cds_arr).squeeze()

        if (self.cds_act_dict is None) or (self.cds_ref_dict is None):
            _log.error('Dictionaries of CDS noise need generating: See `get_cds_dict()`')
            return

        # Temperature array
        temp_arr = np.array(self.temperature_dict[temp_key])

        if temperature is not None:
            if (temperature<temp_arr.min()) or (temperature>temp_arr.max()):
                tbounds = 'T=[{:.2f}, {:.2f}]K'.format(temp_arr.min(), temp_arr.max())
                _log.warn('Requested temperature is outside of bounds: {}.'.format(tbounds))
                _log.warn('Extrapolation may be inaccurate.')

        # CDS dictionary arrays
        d_act = self.cds_act_dict
        d_ref = self.cds_ref_dict

        if 'spat' in cds_type:
            cds_type_list = ['spat']
        elif 'temp' in cds_type:
            cds_type_list = ['temp']
        else:
            cds_type_list = ['spat', 'temp']

        cds_tot = cds_white = 0
        cds_pink_uncorr = cds_pink_corr = 0
        ref_ratio_all = 0
        for ct in cds_type_list:

            # Total noise per channel
            cds_key = f'{ct}_tot'
            if temperature is None:
                cds_tot += np.median(d_act[cds_key], axis=0)
            else:
                cds_tot += cds_fit(temperature, temp_arr, d_act[cds_key])

            # White noise per channel
            cds_key = f'{ct}_white'
            if temperature is None:
                cds_white += np.median(d_act[cds_key], axis=0)
            else:
                cds_white += cds_fit(temperature, temp_arr, d_act[cds_key])

            # 1/f noise per channel
            cds_key = f'{ct}_pink_uncorr'
            cds_pink_uncorr += np.median(d_act[cds_key], axis=0)
            # Correlated noise
            cds_key = f'{ct}_pink_corr'
            cds_pink_corr += np.median(d_act[cds_key])

            # Reference pixel noise ratio
            cds_key = f'{ct}_white' # or f'{cds_type}_tot'?
            ref_ratio_all += (d_ref[cds_key] / d_act[cds_key])

        ref_ratio = np.mean(ref_ratio_all)

        # Scale by number of modes included
        ntype = len(cds_type_list)
        cds_dict = {
            'tot'   : cds_tot / ntype,
            'white' : cds_white / ntype,
            'pink_uncorr' : cds_pink_uncorr / ntype,
            'pink_corr'   : cds_pink_corr / ntype,
            'ref_ratio'   : ref_ratio / ntype
        }

        return cds_dict

    def get_column_variations(self, force=False, **kwargs):
        """ Get column offset variations
        
        Create a series of column offset models.
        These are likely FETS in the ASIC preamp or ADC 
        causing entire columns within a ramp to jump around.
        """

        _log.info("Determining column variations (RTN)")
        allfiles = self.allfiles

        outname = self.paths_dict['column_variations']
        file_exists = os.path.isfile(outname)

        if file_exists and (not force):
            ramp_column_varations, header = get_fits_data(outname, return_header=True)
            prob_bad = header['PROB_VAR']
        else:
            kwargs_def = {
                'nchans': self.nchan, 'altcol': True, 'in_place': True,    
                'fixcol': True, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
            }
            for k in kwargs_def.keys():
                if k not in kwargs:
                    kwargs[k] = kwargs_def[k]

            # Generate a compilation of column variations
            res = gen_col_variations(allfiles, DMS=self.DMS, super_bias=self.super_bias, 
                                     super_dark_ramp=self.super_dark_ramp, **kwargs)
            ramp_column_varations, prob_bad = res
            
            # Save column ramp variations
            hdu = fits.PrimaryHDU(ramp_column_varations)
            hdu.header['PROB_VAR'] = prob_bad
            hdu.writeto(outname, overwrite=True)

        self._column_variations = ramp_column_varations
        self._column_prob_bad   = prob_bad

    def get_ref_pixel_noise(self, force=False, **kwargs):
        """ Generate Dictionary of Reference Pixel behavior info"""

        _log.info("Determining reference pixel behavior")

        allfiles = self.allfiles

        outname = self.paths_dict['ref_pix_variations']
        file_exists = os.path.isfile(outname)

        if file_exists and (not force):
            # Load from JSON file
            with open(outname, 'r') as fp:
                ref_dict = json.load(fp)

            # Convert lists to np.array
            for k in ref_dict.keys():
                if isinstance(ref_dict[k], (list)):
                    ref_dict[k] = np.array(ref_dict[k])
        else:
            kwargs_def = {
                'nchans': self.nchan, 'altcol': True, 'in_place': True,    
                'fixcol': True, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
            }
            for k in kwargs_def.keys():
                if k not in kwargs:
                    kwargs[k] = kwargs_def[k]

            ref_dict = gen_ref_dict(allfiles, self.super_bias, DMS=self.DMS, **kwargs)
            
            # Save to JSON file
            # Make a deepcopy of dict to convert np.array to lists
            dtemp = deepcopy(ref_dict)
            for k in dtemp.keys():
                if isinstance(dtemp[k], (np.ndarray)):
                    dtemp[k] = dtemp[k].tolist()

            with open(outname, 'w') as fp:
                json.dump(dtemp, fp, sort_keys=False, indent=4)

        self._ref_pixel_dict = ref_dict

    def get_power_spectrum(self, include_oh=False, return_corr=False, return_ucorr=False,
                           force=False, save=True, calc_cds=True, per_pixel=False, mn_func=np.mean):

        _log.info("Building noise power spectrum dictionary...")

        # Get file name to save results
        if per_pixel:
            outname = self.paths_dict['power_spec_cds_pix'] if calc_cds else self.paths_dict['power_spec_full_pix']
        else:
            if include_oh:
                outname = self.paths_dict['power_spec_cds_oh'] if calc_cds else self.paths_dict['power_spec_full_oh']
            else:
                outname = self.paths_dict['power_spec_cds'] if calc_cds else self.paths_dict['power_spec_full']
        file_exists = os.path.isfile(outname)

        if file_exists and (not force):
            with open(outname, 'rb') as f:
                ps_all = np.load(f)
                ps_corr = np.load(f)
                ps_ucorr = np.load(f)

        else:
            super_bias = self.super_bias
            if super_bias is None:
                raise AttributeError('Super bias (`self.super_bias = None`) file has not been loaded.')

            ssd = self.det.same_scan_direction
            rsd = self.det.reverse_scan_direction

            res = get_power_spec_all(self.allfiles, super_bias=super_bias, det=self.det,
                                     DMS=self.DMS, include_oh=include_oh, calc_cds=calc_cds,
                                     return_corr=return_corr, return_ucorr=return_ucorr, mn_func=mn_func, 
                                     per_pixel=per_pixel, same_scan_direction=ssd, reverse_scan_direction=rsd)
            ps_all, ps_corr, ps_ucorr = res

            # Set as an arrays of 0s if not calculated for saving purposes
            ps_corr  = np.zeros_like(ps_all[0]).astype('bool') if ps_corr  is None else ps_corr
            ps_ucorr = np.zeros_like(ps_all).astype('bool')    if ps_ucorr is None else ps_ucorr

            # Save arrays to disk            
            if save:
                with open(outname, 'wb') as f:
                    np.save(f, ps_all)
                    np.save(f, ps_corr)
                    np.save(f, ps_ucorr)

        # If corr or ucorr were saved as 0s, set to None
        ps_corr = None if np.allclose(ps_corr, 0) else ps_corr
        ps_ucorr = None if np.allclose(ps_ucorr, 0) else ps_ucorr

        # Get corrsponding frequency arrays
        freq = get_freq_array(ps_all, dt=1/self.det._pixel_rate)
        self._pow_spec_dict = {
            'freq'     : freq,
            'ps_all'   : ps_all,
            'ps_corr'  : ps_corr,
            'ps_ucorr' : ps_ucorr,
        }

        # TODO: Check if something similar for per_pixel
        if not per_pixel:
            # Estimate 1/f scale factors for broken correlated power spectrum
            freq = self.pow_spec_dict['freq']
            ps_all = self.pow_spec_dict['ps_all']

            # Noise values
            cds_dict = self.cds_act_dict
            keys = ['spat_det', 'temp_det', 'spat_pink_uncorr', 'temp_pink_uncorr']
            cds_vals = np.array([np.sqrt(np.mean(cds_dict[k]**2, axis=0)) for k in keys])
            rd_noise = np.sqrt(np.mean(cds_vals[:2]**2))
            u_pink = np.sqrt(np.mean(cds_vals[2:]**2))

            # White Noise
            yf = freq**(0)
            varience = np.mean(rd_noise**2)
            yf1 = len(yf) * varience * yf / yf.sum()

            # Uncorrelated Pink Noise
            yf = freq**(-1); yf[0]=0
            varience = np.mean(u_pink**2) / np.sqrt(2)
            yf2 = len(yf) * varience * yf / yf.sum() 

            # Get residual, to calculate scale factors for correlated noise model
            yresid = ps_all.mean(axis=0) - yf2 - yf1
            scales = fit_corr_powspec(freq, yresid)

            self._pow_spec_dict['ps_corr_scale'] = scales

    def deconvolve_supers(self):
        """
        Deconvolve the super dark and super bias images
        """

        k_ppc = self.kernel_ppc
        k_ipc = self.kernel_ipc
        if (k_ppc is None) and (k_ipc is None):
            _log.error("Neither IPC or PPC kernels are defined")
            return

        _log.info("Deconvolving super dark and super bias images...")

        # PPC Deconvolution
        if k_ppc is not None:
            ssd = self.det.same_scan_direction
            rsd = self.det.reverse_scan_direction
            super_dark_deconv = ppc_deconvolve(self.super_dark, k_ppc,
                                               same_scan_direction=ssd, 
                                               reverse_scan_direction=rsd)
            super_bias_deconv = ppc_deconvolve(self.super_bias, k_ppc,
                                               same_scan_direction=ssd, 
                                               reverse_scan_direction=rsd)

        # IPC Deconvolution
        if k_ipc is not None:
            super_dark_deconv = ipc_deconvolve(super_dark_deconv, k_ipc)
            super_bias_deconv = ipc_deconvolve(super_bias_deconv, k_ipc)

        self._super_dark_deconv = super_dark_deconv
        self._super_bias_deconv = super_bias_deconv

    def plot_bias_darks(self, save=False, return_figax=False, deconvolve=False):
        """
        """
        if self.super_bias is None:
            _log.error("Super bias image has not yet been generated.")
            return
        if self.super_dark is None:
            _log.error("Super dark image has not yet been generated.")
            return

        scaid = self.scaid

        if deconvolve:
            super_bias, super_dark = self.super_bias_deconv, self.super_dark_deconv
        else:
            super_bias, super_dark = self.super_bias, self.super_dark


        fig, axes = plt.subplots(1,2,figsize=(14,8.5), sharey=True)
        cbar_labels = ['Relative Offset (DN)', 'Dark Current (DN/sec)']
        for i, im in enumerate([super_bias, super_dark]):

            mn = np.median(im)
            std = robust.medabsdev(im)

            vmin = mn - 3*std
            vmax = mn + 3*std
            ax = axes[i]
            image = ax.imshow(im, vmin=vmin, vmax=vmax)

            # Add colorbar
            cbar = fig.colorbar(image, ax=ax, orientation='horizontal', 
                                pad=0.05, fraction=0.1, aspect=30, shrink=1)
            cbar.set_label(cbar_labels[i])


        # Add titles and labels
        titles = ['Super Bias Image', 'Super Dark Current Image']
        for i, ax in enumerate(axes):
            ax.set_title(titles[i])

        fig.suptitle(f'SCA {scaid}', fontsize=16)

        fig.tight_layout()
        fig.subplots_adjust(top=0.92, wspace=0.02, bottom=0.01)

        if save:
            fname = f'{scaid}_bias_dark_images.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_dark_ramps(self, save=True, time_cut=None, return_figax=False):
        """ Plot average dark current ramps
        
        time_cut : float
            Some darks show distinct slopes before and after a 
            characteristic time. Setting this keyword will fit
            separate slopes before and after the specified time.
            A time of 200 sec is used for SCA 485.
        """

        # Make sure dictionary is not empty
        if self._dark_ramp_dict is None:
            self.get_pixel_slope_averages()
        scaid = self.scaid

        fig, axes = plt.subplots(1,2, figsize=(14,5))
        axes = axes.flatten()

        # Plot average of all pixel
        ax = axes[0]
        ax.set_title('Average Ramp of All Pixels')
        tarr = self.time_arr
        y = self.dark_ramp_dict['ramp_avg_all']
        ax.plot(tarr, y, marker='.', label='Median Pixel Values')

        if time_cut is None:
            cf = jl_poly_fit(tarr, y, deg=1)
            ax.plot(tarr, jl_poly(tarr,cf), label='Slope Fit = {:.4f} DN/sec'.format(cf[1]))
        else:
            for ind in [tarr<time_cut, tarr>time_cut, tarr>0]:
                cf = jl_poly_fit(tarr[ind], y[ind], deg=1)
                ax.plot(tarr, jl_poly(tarr,cf), label='Slope Fit = {:.4f} DN/sec'.format(cf[1]))

        # Plot each channel separately
        ax = axes[1]
        ax.set_title('Channel Ramps')
        for i in range(self.nchan):
            y = self.dark_ramp_dict['ramp_avg_ch'][i]
            cf = jl_poly_fit(tarr, y, deg=1)
            label = 'Ch{} = {:.4f} DN/sec'.format(i, cf[1])
            ax.plot(tarr, y, marker='.', label=label)
            
        ylim1 = ylim2 = 0
        for ax in axes:
            ax.set_xlabel('Time (sec)')
            ax.set_ylabel('Signal (DN)')
            ax_yl = ax.get_ylim()
            ylim1 = np.min([ylim1, ax_yl[0]])
            ylim2 = np.max([ylim2, ax_yl[1]])
            ax.legend()

        for ax in axes:
            ax.set_ylim([ylim1,ylim2])
            # Plot baseline at y=0
            xlim = ax.get_xlim()
            ax.plot(xlim, [0,0], color='k', ls='--', lw=1, alpha=0.25)
            ax.set_xlim(xlim)

        fig.suptitle(f'Dark Current (SCA {scaid})', fontsize=16)
            
        fig.tight_layout()
        fig.subplots_adjust(top=0.85)

        if save:
            fname = f'{scaid}_dark_ramp_avg.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_dark_ramps_ch(self, save=True, time_cut=None, return_figax=False):
        """ Plot fits to each channel dark current ramp
        
        time_cut : float
            Some darks show distinct slopes before and after a 
            characteristic time. Setting this keyword will fit
            separate slopes before and after the specified time.
            A time of 200 sec is used for SCA 485.
        """

        # Make sure dictionary is not empty
        if self._dark_ramp_dict is None:
            self.get_pixel_slope_averages()
        scaid = self.scaid

        fig, axes = plt.subplots(2,2, figsize=(14,9))
        axes = axes.flatten()
            
        # Plot Individual Channels
        tarr = self.time_arr
        for i in range(self.nchan):
            ax = axes[i]
            y = self.dark_ramp_dict['ramp_avg_ch'][i]
            ax.plot(tarr, y, marker='.', label='Pixel Averages')

            if time_cut is None:
                cf = jl_poly_fit(tarr, y, deg=1)
                ax.plot(tarr, jl_poly(tarr,cf), label='Slope = {:.4f} DN/sec'.format(cf[1]))
            else:
                for ind in [tarr<time_cut, tarr>time_cut, tarr>0]:
                    cf = jl_poly_fit(tarr[ind], y[ind], deg=1)
                    ax.plot(tarr, jl_poly(tarr,cf), label='Slope = {:.4f} DN/sec'.format(cf[1]))

            ax.set_title(f'Amplifier Channel {i}')

        ylim1 = ylim2 = 0
        for ax in axes:
            ax.set_xlabel('Time (sec)')
            ax.set_ylabel('Dark Value (DN)')
            ax_yl = ax.get_ylim()
            ylim1 = np.min([ylim1, ax_yl[0]])
            ylim2 = np.max([ylim2, ax_yl[1]])
            ax.legend()

        for ax in axes:
            ax.set_ylim([ylim1,ylim2])
            # Plot baseline at y=0
            xlim = ax.get_xlim()
            ax.plot(xlim, [0,0], color='k', ls='--', lw=1, alpha=0.25)
            ax.set_xlim(xlim)
            
        fig.suptitle(f'Dark Current (SCA {scaid})', fontsize=16)
            
        fig.tight_layout()
        fig.subplots_adjust(top=0.9)

        if save:
            fname = f'{scaid}_dark_ramp_chans.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_dark_distribution(self, save=False, xlim=None, return_figax=False):
        """Plot histogram of dark slope"""

        act_mask = self.mask_act
        ch_mask = self.mask_channels
        nchan = self.nchan
        scaid = self.scaid

        # Histogram of Dark Slope
        if self.super_dark is None:
            _log.error("Super dark image has not yet been generated.")
            return

        fig, axes = plt.subplots(1,2, figsize=(14,5), sharey=True)

        # Full image
        ax = axes[0]
        im = self.super_dark[act_mask]
        plot_dark_histogram(im, ax)

        # Individual Amplifiers
        ax = axes[1]
        carr = ['C0', 'C1', 'C2', 'C3']
        for ch in np.arange(nchan):
            ind = (ch_mask==ch) & act_mask
            im = self.super_dark[ind]
            label = f'Ch{ch}'
            plot_dark_histogram(im, ax, label=label, color=carr[ch], 
                                 plot_fit=False, plot_cumsum=False)
        ax.set_ylabel('')
        ax.set_title('Active Pixels per Amplifier')

        # Plot baseline at y=0
        for ax in axes:
            if xlim is None:
                xlim = ax.get_xlim()
            ax.plot(xlim, [0,0], color='k', ls='--', lw=1, alpha=0.25)
            ax.set_xlim(xlim)

        fig.suptitle(f'Dark Current Distriutions (SCA {self.scaid})', fontsize=16)
        fig.tight_layout()
        fig.subplots_adjust(top=0.85, wspace=0.025)

        if save:
            fname = f'{self.scaid}_dark_histogram.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_dark_overview(self, save=False, xlim_hist=None, return_figax=False):
        """Plot Overview of Dark Current Characteristics"""
        
        if self.super_dark is None:
            _log.error("Super dark image has not yet been generated.")
            return
        
        scaid = self.scaid
        fig, axes = plt.subplots(1,3,figsize=(14,5))

        #########################################
        # Dark Current slope image
        ax = axes[0]

        im = self.super_dark
        mn = np.median(im)
        std = robust.medabsdev(im)

        vmin = mn - 3*std
        vmax = mn + 3*std
        image = ax.imshow(im, vmin=vmin, vmax=vmax)

        # Add colorbar
        cbar = fig.colorbar(image, ax=ax, orientation='horizontal',
                            pad=0.08, fraction=0.05, aspect=30, shrink=0.9)
        ax.set_title('Dark Current Image')
        cbar.set_label('Dark Current (DN/sec)')

        #########################################
        # Average pixel slope over time
        ax = axes[1]

        ax.set_title('Average Ramp of All Pixels')
        tarr = self.time_arr
        y = self.dark_ramp_dict['ramp_avg_all']
        ax.plot(tarr, y, marker='.', label='Median Pixel Values')

        cf = jl_poly_fit(tarr, y, deg=1)
        ax.plot(tarr, jl_poly(tarr,cf), label='Slope Fit = {:.4f} DN/sec'.format(cf[1]))

        # Plot baseline at y=0
        xlim = ax.get_xlim()
        ax.plot(xlim, [0,0], color='k', ls='--', lw=1, alpha=0.25)
        ax.set_xlim(xlim)

        ax.set_xlabel('Time (sec)')
        ax.set_ylabel('Signal (DN)')
        ax.legend()

        #########################################
        # Dark current histogram
        ax = axes[2]

        act_mask = self.mask_act
        scaid = self.scaid

        # Histogram of Dark Slope
        im = self.super_dark[act_mask]

        ax = plot_dark_histogram(im, ax, return_ax=True, plot_fit=False)
        ax.set_title('Slope Distribution')

        # Plot baseline at y=0
        if xlim_hist is None:
            xlim_hist = ax.get_xlim()
        ax.plot(xlim_hist, [0,0], color='k', ls='--', lw=1, alpha=0.25)
        ax.set_xlim(xlim_hist)

        fig.suptitle(f'Dark Current Overview (SCA {scaid})', fontsize=16)

        fig.tight_layout()
        fig.subplots_adjust(bottom=0.1, top=0.85)

        if save:
            fname = f'{self.scaid}_dark_overview.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_ipc_ppc(self, k_ipc=None, k_ppc=None, save=False, return_figax=False):

        k_ipc = self.kernel_ipc if k_ipc is None else k_ipc
        k_ppc = self.kernel_ppc if k_ppc is None else k_ppc
        scaid = self.scaid
        
        if k_ipc is None:
            _log.info("IPC Kernel does not exist.")
            return

        if k_ipc is None:
            # Plot only IPC kernel
            fig, axes = plt.subplots(1,1, figsize=(5,5))
            plot_kernel(k_ipc, ax=axes)
            axes.set_title('IPC Kernel', fontsize=16)
            fig.tight_layout()
        else:
            # Plot both IPC and PPC
            fig, axes = plt.subplots(1,2, figsize=(10,5.5), sharey=True)

            ax = axes[0]
            plot_kernel(k_ipc, ax=ax)
            ax.set_title('IPC Kernel')

            ax = axes[1]
            plot_kernel(k_ppc, ax=ax)
            ax.set_title('PPC Kernel')

            fig.suptitle(f"Pixel Deconvolution Kernels (SCA {scaid})", fontsize=16)

            fig.tight_layout()
            fig.subplots_adjust(wspace=0.075, top=0.9)
        
        if save:
            fname = f'{self.scaid}_pixel_kernels.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes

    def plot_reset_overview(self, save=False, binsize=0.25, xlim_hist=None,
                            return_figax=False):
        """ Overview Plots of Bias and kTC Noise"""

        if self._super_bias is None:
            _log.error('Super bias image does not exist.')
            return

        if self._super_bias_sig is None:
            _log.error('Sigma image for super bias does not exist.')
            return

        scaid = self.scaid

        # Histogram of Bias kTC
        im = self._super_bias_sig[self.mask_act]
        binsize = binsize
        bins = np.arange(im.min(), im.max() + binsize, binsize)
        ig, vg, cv = hist_indices(im, bins=bins, return_more=True)

        nvals = np.array([len(i) for i in ig])
        nvals_rel = nvals / nvals.max()

        # Peak of distribution
        if self.ktc_noise is None:
            self.get_ktc_noise(binsize=binsize)
        peak = self.ktc_noise

        fig, axes = plt.subplots(1,3,figsize=(14,5))

        #####################################
        # Plot super bias image
        ax = axes[0]

        im = self._super_bias
        mn = np.median(im)
        std = robust.std(im)

        ax.imshow(im, vmin=mn-3*std, vmax=mn+3*std)
        ax.set_title('Super Bias Image')

        #####################################
        # Plot kTC noise image
        ax = axes[1]

        im = self._super_bias_sig
        mn = np.median(im)
        std = robust.std(im)

        ax.imshow(im, vmin=mn-3*std, vmax=mn+3*std)
        ax.set_title('kTC Noise = {:.1f} DN'.format(peak))

        #####################################
        # Plot kTC noise histogram
        ax = axes[2]
        ax.plot(cv, nvals_rel, label='Measured Noise')

        label = 'Peak ({:.1f} DN)'.format(peak)
        ax.plot(np.array([1,1])*peak, [0,1], ls='--', lw=1, label=label)
        ncum = np.cumsum(nvals) 
        ax.plot(cv, ncum / ncum.max(), color='C3', lw=1, label='Cumulative Sum')

        ax.set_title('kTC Noise Distribution')
        ax.set_xlabel('Bias Noise (DN)')
        ax.set_ylabel('Relative Number of Pixels')
        ax.legend()

        ax.set_xlim([0,3*peak])
        #ax.xaxis.get_major_locator().set_params(nbins=9, steps=[1, 2, 5, 10])

        # Plot baseline at y=0
        if xlim_hist is None:
            xlim_hist = ax.get_xlim()
        ax.plot(xlim_hist, [0,0], color='k', ls='--', lw=1, alpha=0.25)
        ax.set_xlim(xlim_hist)


        fig.suptitle(f'Reset Bias Overview (SCA {scaid})', fontsize=16)
        fig.tight_layout()
        fig.subplots_adjust(bottom=0.1, top=0.85)

        if save:
            fname = f'{self.scaid}_bias_overview.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes

    def plot_cds_noise(self, tkey='T_FPA1', save=False, return_figax=False,
        xlim=[36.1,40.1]):

        fig, axes = plt.subplots(2,3, figsize=(14,8), sharey=True)

        temp_arr = np.array(self.temperature_dict[tkey])

        d = self.cds_act_dict
        d2 = self.cds_ref_dict

        # 1. Total Noise
        k1, k2 = ('spat_tot', 'temp_tot')
        for k, ax in zip([k1,k2], axes[:,0]):
            
            cds_arr = d[k]
            for ch in np.arange(self.nchan):
                ax.plot(temp_arr, cds_arr[:,ch], marker='o', ls='none', label=f'Ch{ch}')
                    
            type_str = "Spatial" if 'spat' in k else "Temporal"
            title_str = f"{type_str} Total Noise"
            ax.set_title(title_str)
        
        # 2. White Noise
        k1, k2 = ('spat_det', 'temp_det')
        cmap = plt.get_cmap('tab20')
        tplot = np.array([temp_arr.min(), temp_arr.max()])
        for k, ax in zip([k1,k2], axes[:,1]):
            
            cds_arr = d[k]
            pix_type = ['Active', 'Ref']
            for j, cds_arr in enumerate([d[k], d2[k]]):
                marker = 'o' if j==0 else '.'
                for ch in np.arange(self.nchan):
                    label = f'Ch{ch} ({pix_type[j]})'
                    y = cds_arr[:,ch]
                    ax.plot(temp_arr, y, marker=marker, ls='none', label=label, color=cmap(ch*2+j))
                    cf = jl_poly_fit(temp_arr, y)
                    ax.plot(tplot, jl_poly(tplot, cf), lw=1, ls='--', color=cmap(ch*2+j))
                        
            type_str = "Spatial" if 'spat' in k else "Temporal"
            title_str = f"{type_str} White Noise"
            ax.set_title(title_str)
    
        # 3. Pink Noise
        k1, k2 = ('spat_pink_uncorr', 'temp_pink_uncorr')
        for k, ax in zip([k1,k2], axes[:,2]):
            
            cds_arr = d[k]
            for ch in np.arange(self.nchan):
                ax.plot(temp_arr, cds_arr[:,ch], marker='o', ls='none', label=f'Ch{ch}')
            
            k_corr = k.split('_')
            k_corr[-1] = 'corr'
            k_corr = '_'.join(k_corr)
            
            ax.plot(temp_arr, d[k_corr], marker='o', ls='none', label='Correlated')

            type_str = "Spatial" if 'spat' in k else "Temporal"
            title_str = f"{type_str} 1/f Noise"
            ax.set_title(title_str)

        for ax in axes:
            ax[0].set_ylabel('CDS Noise (DN)')
        for ax in axes[-1,:]:
            ax.set_xlabel('FPA Temperature (K)')
        for ax in axes.flatten():
            ax.set_xlim(xlim)
            handles, labels = ax.get_legend_handles_labels()
            ncol = 2 if len(handles)>5 else 1
            ax.legend(ncol=ncol)
        
        fig.suptitle(f'CDS Noise Overview (SCA {self.scaid})', fontsize=16)
        fig.tight_layout()
        fig.subplots_adjust(wspace=0.01, top=0.9)

        if save:
            fname = f'{self.scaid}_cds_noise.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes

    def plot_eff_noise(self, ideal_Poisson=False, save=False, return_figax=False):
        """Plot effective noise of slope fits"""

        det = self.det
        gain = det.gain
        nchan = det.nout

        # Average dark current (e-/sec)
        if self.dark_ramp_dict is None:
            idark = np.ones(nchan) * det.dark_current   # e-/sec
        else:
            idark = []
            tarr = self.time_arr
            for ch in np.arange(nchan):
                y = self.dark_ramp_dict['ramp_avg_ch'][ch]
                cf = jl_poly_fit(tarr, y, deg=1)
                idark.append(cf[1])
            idark = np.array(idark) * gain   # e-/sec

        eff_noise_dnsec = self.eff_noise_dict['en_spat_list'][0]
        # Average read noise per frame (e-)
        cds_var = (eff_noise_dnsec[0:nchan,0] * det.time_group * gain)**2 - (idark * det.time_group)
        read_noise = np.sqrt(cds_var / 2) # e-
        read_noise_ref = eff_noise_dnsec[-1,0] * det.time_group * gain / np.sqrt(2)

        ng_all = self.eff_noise_dict['ng_all_list'][0]
        tvals = self.eff_noise_dict['tarr_all_list'][0]
        p_excess = self.eff_noise_dict['p_excess']

        colarr = ['C0', 'C1', 'C2', 'C3', 'C4']
        fig, axes = plt.subplots(1,2, figsize=(14,4.5))

        ax = axes[0]

        # Measured Values
        xvals = tvals
        yvals = eff_noise_dnsec
        for ch in range(nchan):
            axes[0].plot(xvals, yvals[ch]*tvals, marker='o', label=f'Ch{ch} - Meas', color=colarr[ch])
            axes[1].semilogy(xvals, yvals[ch], marker='o', label=f'Ch{ch} - Meas', color=colarr[ch])
        ch = -1
        axes[0].plot(xvals, yvals[ch]*tvals, marker='o', label='Ref - Meas', color=colarr[ch])
        axes[1].plot(xvals, yvals[ch], marker='o', label='Ref - Meas', color=colarr[ch])

        # Theoretical Values
        xvals = tvals
        for ch in range(nchan):
            thr_e = det.pixel_noise(ng=ng_all, rn=read_noise[ch], idark=idark[ch], 
                                        ideal_Poisson=ideal_Poisson, p_excess=p_excess)
            yvals2 = (thr_e * tvals) / gain
            axes[0].plot(xvals, yvals2,  color=colarr[ch], lw=10, alpha=0.3, label=f'Ch{ch} - Theory')
            axes[1].plot(xvals, yvals2/tvals,  color=colarr[ch], lw=10, alpha=0.3, label=f'Ch{ch} - Theory')
        ch = -1
        thr_e = det.pixel_noise(ng=ng_all, rn=read_noise_ref, idark=0, p_excess=[0,0])
        yvals2 = (thr_e * tvals) / gain
        axes[0].plot(xvals, yvals2,  color=colarr[ch], lw=10, alpha=0.3, label=f'Ref - Theory')
        axes[1].plot(xvals, yvals2/tvals,  color=colarr[ch], lw=10, alpha=0.3, label=f'Ref - Theory')

        ax = axes[0]
        ax.set_ylim([0,ax.get_ylim()[1]])
        axes[0].set_ylabel('Effective Noise (DN)')
        axes[1].set_ylabel('Slope Noise (DN/sec)')
        for ax in axes:
            ax.set_xlabel('Time (sec)')
        #ax.set_title(f'Effective Noise (SCA {self.scaid})')

        axes[0].legend(ncol=2)

        fig.suptitle(f"Noise of Slope Fits (SCA {self.scaid})", fontsize=16)

        fig.tight_layout()
        fig.subplots_adjust(top=0.9)

        if save:
            fname = f'{self.scaid}_eff_noise.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_eff_noise_patterns(self, ideal_Poisson=False, save=False, return_figax=False):
        """Plot effective noise of slope fits for variety of read patterns"""

        det = self.det
        gain = det.gain
        nchan = det.nout
        patterns = list(det.multiaccum._pattern_settings.keys())

        en_spat_list = self.eff_noise_dict['en_spat_list']
        en_dn_list = []
        for i in range(len(patterns)):
            # Average spatial and temporal values
            var_avg_ch = en_spat_list[i]**2
            en_dn_list.append(np.sqrt(var_avg_ch[0:nchan].mean(axis=0)))

        tarr_all = self.eff_noise_dict['tarr_all_list']
        ng_all_list = self.eff_noise_dict['ng_all_list']
        p_excess = self.eff_noise_dict['p_excess']

        # Average dark current (e-/sec)
        if self.dark_ramp_dict is None:
            idark_avg = det.dark_current
        else:
            idark = []
            tarr = self.time_arr
            for ch in np.arange(nchan):
                y = self.dark_ramp_dict['ramp_avg_ch'][ch]
                cf = jl_poly_fit(tarr, y, deg=1)
                idark.append(cf[1])
            idark = np.array(idark) * gain
            idark_avg = np.mean(idark)

        # Average read noise per frame (e-)
        cds_var = (en_dn_list[0][0] * det.time_group * gain)**2 - (idark_avg * det.time_group)
        read_noise = np.sqrt(cds_var / 2)

        fig, axes = plt.subplots(3,3, figsize=(14,9), sharey=True)
        axes = axes.flatten()

        for i, ax in enumerate(axes):
            tvals = tarr_all[i]
            yvals = (en_dn_list[i] * tvals)

            xvals = tvals
            ax.plot(xvals, yvals, marker='o', label='Measured')
            
            det_new = deepcopy(det)
            ma_new = det_new.multiaccum
            ma_new.read_mode = patterns[i]

            ng_all = ng_all_list[i]
            thr_e = det_new.pixel_noise(ng=ng_all, rn=read_noise, idark=idark_avg, 
                                        ideal_Poisson=ideal_Poisson, p_excess=[0,0])
            
            yvals = (thr_e * tvals) / gain
            ax.plot(xvals, yvals, color='C1', label='Theory')

            tvals = tarr_all[i]
            ng_all = ng_all_list[i]
            thr_e = det_new.pixel_noise(ng=ng_all, rn=read_noise, idark=idark_avg, 
                                        ideal_Poisson=ideal_Poisson, p_excess=p_excess)
            
            yvals = (thr_e * tvals) / gain
            ax.plot(xvals, yvals, marker='.', color='C1', ls='--', label='Theory + Excess')

        for i, ax in enumerate(axes):
            if i==0:
                xr = [ax.get_xlim()[0],1200]
                ymax = 5*(int(ax.get_ylim()[1] / 5) + 1)
                yr = [0,ymax]
                
            ax.set_xlim(xr)
            ax.set_ylim(yr)
            ax.set_title(patterns[i])
            
            if i>5:
                ax.set_xlabel('Time (sec)')
            if np.mod(i,3) == 0:
                ax.set_ylabel('Noise (DN)')
            
        # Legend on first plot
        axes[0].legend()

        fig.suptitle(f'Noise of Slope Fits (SCA {self.scaid})', fontsize=16)
        fig.tight_layout()
        fig.subplots_adjust(top=0.9, wspace=0.03)

        if save:
            fname = f'{self.scaid}_eff_noise_patterns.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name)

        if return_figax:
            return fig, axes


    def plot_power_spectrum(self, save=False, cds=True, return_figax=False):

        scaid = self.scaid

        cds_dict = self.cds_act_dict
        keys = ['spat_det', 'temp_pink_corr', 'temp_pink_uncorr']
        cds_vals = [np.sqrt(np.mean(cds_dict[k]**2, axis=0)) for k in keys]
        rd_noise_cds, c_pink_cds, u_pink_cds = cds_vals

        nchan = self.nchan

        freq = self.pow_spec_dict['freq']
        ps_all = self.pow_spec_dict['ps_all']

        fig, axes = plt.subplots(1,2, figsize=(14,5))

        ax = axes[0]
        
        # Amplifier averages
        x = freq
        y = np.mean(ps_all, axis=0)
        label='Amplifier Averaged'
        ax.loglog(x[1:], y[1:], marker='o', ms=0.25, ls='none', color='grey', 
                  label=label, rasterized=True)

        # White Noise
        yf = x**(0)
        cds_var = np.mean(rd_noise_cds**2)
        yf1 = len(yf) * cds_var * yf / yf.sum() 
        ax.plot(x[1:], yf1[1:], ls='--', lw=1, label='White Noise')

        # Pink Noise per Channel
        yf = x**(-1); yf[0]=0
        cds_var = np.mean(u_pink_cds**2) / np.sqrt(2)
        yf2 = len(yf) * cds_var * yf / yf.sum()
        ax.plot(x[1:], yf2[1:], ls='--', lw=1, label='Uncorr Pink Noise')

        # Correlated Pink Noise
        yresid = y - yf2 - yf1
        scales = fit_corr_powspec(x, yresid)
        yf = broken_pink_powspec(x, scales)
        cds_var = c_pink_cds**2 / np.sqrt(2)
        yf3 = len(yf) * cds_var * yf / yf.sum() 
        ax.plot(x[1:], yf3[1:], ls='--', lw=1, label='Corr Pink Noise')

        # Total of the three components
        yf_sum = (yf1 + yf2 + yf3) 
        ax.plot(x[1:], yf_sum[1:], ls='--', lw=2, label='Sum')

        ax.set_ylabel('CDS Power (DN$^2$)')

        ax = axes[1]

        x = freq
        for ch in range(nchan):
            y = ps_all[ch]
            ax.loglog(x[1:], y[1:], marker='o', ms=0.25, ls='none', 
                      label=f'Ch{ch}', rasterized=True)
            
        for ax in axes:
            ax.set_xlim([5e-2, 7e4])
            xloc = np.array(ax.get_xticks())
            xlim = ax.get_xlim()
            xind = (xloc>=xlim[0]) & (xloc<=xlim[1])
            ax.set_xlabel('Frequency (Hz)')
            
            ax.set_xlim(xlim)
            ax.set_ylim([10,1e7])
            ax.legend(numpoints=3, markerscale=10)

            ax2 = ax.twiny()
            ax2.set_xlim(1/np.array(xlim))
            ax2.set_xscale('log')
            ax2.set_xlabel('Time (sec)')
            # new_tick_locations = xloc[xind]
            # ax2.set_xticks(new_tick_locations)
            # ax2.set_xticklabels(tick_function(new_tick_locations))

            ax.minorticks_on()


        fig.suptitle(f'Noise Power Spectrum (SCA {scaid})', fontsize=16)

        fig.tight_layout()
        fig.subplots_adjust(top=0.85)

        if save:
            fname = f'{scaid}_power_spectra.pdf'
            save_name = os.path.join(self.paths_dict['figdir'], fname)
            _log.info(f"Saving to {save_name}")
            fig.savefig(save_name, dpi=150)

        if return_figax:
            return fig, axes


def get_fits_data(fits_file, return_header=False, bias=None,
                  reffix=False, DMS=False, int_ind=0, **kwargs):
    
    """
    Parameters
    ==========
    fname : str
        FITS file (including path) to open.
    return_header : bool
        Return header as well as data?
    bias : ndarray
        If specified, will subtract bias image from ramp.
    reffix : bool
        Perform reference correction?
    DMS : bool
        Is the FITS file DMS format?
    int_ind : int
        If DMS format, select integration index to extract.
        DMS FITS files usually have all integrations within
        a given exposure in a single FITS extension, which
        can be quite large.
    
    Keyword Args
    ============
    'nchans': nchan, 
    'altcol': True, 
    'in_place': True,    
    'fixcol': True, 
    'avg_type': 'pixel', 
    'savgol': True, 
    'perint': False    
    """
    
    # Want to automatically determine if FITS files have DMS structure
    hdul = fits.open(fits_file)
    hdr = hdul[0].header

    if DMS:
        if int_ind > hdr['NINTS']-1:
            hdul.close()
            nint = hdr['NINTS']
            raise ValueError(f'int_num must be less than {nint}.')

        data = hdul[1].data[int_ind].astype(np.float)
    else:
        data = hdul[0].data.astype(np.float)
    hdul.close()

    if bias is not None:
        data -= bias
    
    if reffix:
        data = reffix_hxrg(data, **kwargs)

    if return_header:
        return data, hdr
    else:
        return data

def _wrap_super_bias_for_mp(arg):
    args, kwargs = arg

    fname = args[0]
    data, hdr = get_fits_data(fname, return_header=True, reffix=True, **kwargs)

    # Get header information and create a NIRCam detector timing instance
    det = create_detops(hdr, DMS=kwargs['DMS'])

    nz = det.multiaccum.ngroup
    # Time array
    tarr = np.arange(1, nz+1) * det.time_group

    deg = kwargs['deg']
    cf = jl_poly_fit(tarr, data, deg=deg)

    return cf[0]

def gen_super_bias(allfiles, DMS=False, mn_func=np.median, std_func=robust.std, 
                   return_std=False, deg=1, nsplit=3, **kwargs):
    """ Generate a Super Bias Image

    Read in a number of dark ramps, 
    """
    
    # Set logging to WARNING to suppress messages
    log_prev = pynrc.conf.logging_level
    pynrc.setup_logging('WARNING', verbose=False)

    kw = kwargs.copy()
    kw['deg'] = deg
    kw['DMS'] = DMS
    if DMS:
        worker_args = []
        for f in allfiles:
            hdr = fits.getheader(f)
            # Account for multiple ints in each file
            for i in range(hdr['NINTS']):
                kw['int_ind'] = i
                worker_args.append(([f],kw))
    else:
        worker_args = [([f],kw) for f in allfiles]
    
    nfiles = len(allfiles)
            
    if nsplit>1:
        bias_all = []
        # pool = mp.Pool(nsplit)
        try:
            with mp.Pool(nsplit) as pool:
                for res in tqdm(pool.imap_unordered(_wrap_super_bias_for_mp, worker_args), total=nfiles):
                    bias_all.append(res)
                pool.close()

            # bias_all = pool.map(_wrap_super_bias_for_mp, worker_args)
            if bias_all[0] is None:
                raise RuntimeError('Returned None values. Issue with multiprocess??')
        except Exception as e:
            print('Caught an exception during multiprocess.')
            print('Closing multiprocess pool.')
            pool.terminate()
            pool.close()
            raise e
        else:
            print('Closing multiprocess pool.')
            # pool.close()

        bias_all = np.array(bias_all)
    else:
        bias_all = np.array([_wrap_super_bias_for_mp(wa) for wa in tqdm(worker_args)])

    # Set back to previous logging level
    pynrc.setup_logging(log_prev, verbose=False)

    super_bias = mn_func(bias_all, axis=0)
    if return_std:
        _super_bias = std_func(bias_all,axis=0)
        return super_bias, _super_bias
    else:
        return super_bias


def chisqr_red(yvals, yfit=None, err=None, dof=None,
               err_func=np.std):
    """ Calculate reduced chi square metric
    
    If yfit is None, then yvals assumed to be residuals.
    In this case, `err` should be specified.
    
    Parameters
    ==========
    yvals : ndarray
        Sampled values.
    yfit : ndarray
        Model fit corresponding to `yvals`.
    dof : int
        Number of degrees of freedom (nvals - nparams - 1).
    err : ndarray or float
        Uncertainties associated with `yvals`. If not specified,
        then use yvals point-to-point differences to estimate
        a single value for the uncertainty.
    err_func : func
        Error function uses to estimate `err`.
    """
    
    if (yfit is None) and (err is None):
        print("Both yfit and err cannot be set to None.")
        return
    
    diff = yvals if yfit is None else yvals - yfit
    
    sh_orig = diff.shape
    ndim = len(sh_orig)
    if ndim==1:
        if err is None:
            err = err_func(yvals[1:] - yvals[0:-1]) / np.sqrt(2)
        dev = diff / err
        chi_tot = np.sum(dev**2)
        dof = len(chi_tot) if dof is None else dof
        chi_red = chi_tot / dof
        return chi_red
    
    # Convert to 2D array
    if ndim==3:
        sh_new = [sh_orig[0], -1]
        diff = diff.reshape(sh_new)
        yvals = yvals.reshape(sh_new)
        
    # Calculate errors for each element
    if err is None:
        err_arr = np.array([yvals[i+1] - yvals[i] for i in range(sh_orig[0]-1)])
        err = err_func(err_arr, axis=0) / np.sqrt(2)
        del err_arr
    else:
        err = err.ravel()
    # Get reduced chi sqr for each element
    dof = sh_orig[0] if dof is None else dof
    chi_red = np.sum((diff / err)**2, axis=0) / dof
    
    if ndim==3:
        chi_red = chi_red.reshape(sh_orig[-2:])
        
    return chi_red

def ramp_derivative(y, dx=None, fit0=True, deg=2, ifit=[0,10]):

    sh_orig = y.shape
    ndim = len(sh_orig)

    if ndim==1:
        dy = y[1:] - y[:-1]
        
        if fit0:
            xtemp = np.arange(len(dy))+1
            lxmap = [np.min(xtemp), np.max(xtemp)]
            i1, i2 = ifit
            
            xfit = xtemp[i1:i2+1]
            dyfit = dy[i1:i2+1]
            
            # First try to fit log/log
            xfit_log = np.log10(xfit+1)
            dyfit_log = np.log10(dyfit)
            
            # if there are no NaNs, then fit to log scale
            if not np.isnan(dyfit_log.sum()):
                lxmap_log = np.log10(lxmap)
                cf = jl_poly_fit(xfit_log, dyfit_log, deg=deg, use_legendre=True, lxmap=lxmap_log)
                dy0_log = jl_poly(0, cf, use_legendre=True, lxmap=lxmap_log)
                dy0 = 10**dy0_log
            else:            
                cf = jl_poly_fit(xtemp[i1:i2+1], dy[i1:i2+1], deg=deg, use_legendre=True, lxmap=lxmap)
                dy0 = jl_poly(0, cf, use_legendre=True, lxmap=lxmap)
        else:
            dy0 = 2*dy[0] - dy[1]

        dy = np.insert(dy, 0, dy0)

        if dx is not None:
            dy /= dx
            
        return dy
    
    # If fitting multiple pixels simultaneously
    # Convert to 2D array
    elif ndim==3:
        sh_new = [sh_orig[0], -1]
        y = y.reshape(sh_new)

    # Get differential
    dy = y[1:] - y[:-1]
    
    # Fit to slope to determine derivative of first element
    if fit0:
        xtemp = np.arange(len(dy))+1
        lxmap = [np.min(xtemp), np.max(xtemp)]
        i1, i2 = ifit

        # Value on which to perform fit
        xfit = xtemp[i1:i2+1]
        dyfit = dy[i1:i2+1]

        # First try to fit in log/log space
        xfit_log = np.log10(xfit+1)
        dyfit_log = np.log10(dyfit)

        # Variable to hold first element of differential
        dy0 = np.zeros([dy.shape[-1]])

        # Filter pixels that have valid values in logspace
        indnan = np.isnan(dyfit_log.sum(axis=0))
        # Fit invalid values in linear space
        cf = jl_poly_fit(xfit, dyfit[:,indnan], deg=deg, use_legendre=True, lxmap=lxmap)
        dy0[indnan] = jl_poly(0, cf, use_legendre=True, lxmap=lxmap)
        # Fit non-NaN'ed data in logspace
        if len(indnan[~indnan])>0:
            lxmap_log = np.log10(lxmap)
            cf = jl_poly_fit(xfit_log, dyfit_log[:,~indnan], deg=deg, use_legendre=True, lxmap=lxmap_log)
            dy0_log = jl_poly(0, cf, use_legendre=True, lxmap=lxmap_log)
            dy0[~indnan] = 10**dy0_log
    else:
        dy0 = 2*dy[0] - dy[1]
        
    dy = np.insert(dy, 0, dy0, axis=0)

    if ndim==3:
        dy = dy.reshape(sh_orig)

    if dx is not None:
        dy /= dx

    return dy

def gen_super_dark(allfiles, super_bias=None, DMS=False, **kwargs):
    
    # Set logging to WARNING to suppress messages
    log_prev = pynrc.conf.logging_level
    pynrc.setup_logging('WARNING', verbose=False)

    if super_bias is None:
        super_bias = 0
        
    nfiles = len(allfiles)
    
    # Header info from first file
    hdr = fits.getheader(allfiles[0])
    det = create_detops(hdr)

    # nchan = det.nout
    nx = det.xpix
    ny = det.ypix
    nz = det.multiaccum.ngroup
    # chsize = det.chsize

    tarr = np.arange(1, nz+1) * det.time_group

    # Active and reference pixel masks
    lower, upper, left, right = det.ref_info

    mask_ref = np.zeros([ny,nx], dtype='bool')
    mask_ref[0:lower,:] = True
    mask_ref[-upper:,:] = True
    mask_ref[:,0:left] = True
    mask_ref[:,-right:] = True
    mask_act = ~mask_ref

    # mask_act = np.zeros([ny,nx]).astype('bool')
    # mask_act[4:-4,4:-4] = True
    # mask_ref = ~mask_act
    
    # TODO: Better algorithms to find bad pixels
    # See Bad_pixel_changes.pdf from Karl
    masks_dict = {
        'mask_ref': [],
        'mask_poly': [],
        'mask_deviant': [],
        'mask_negative': [],
        'mask_others': []
    }
    bias_off_all = []
    
    # Create a super dark ramp
    ramp_sum = np.zeros([nz,ny,nx])
    ramp_sum2 = np.zeros([nz,ny,nx])
    nsum = np.zeros([ny,nx])
    for fname in tqdm(allfiles):

        # If DMS, then might be multiple integrations per FITS file
        nint = fits.getheader(fname)['NINTS'] if DMS else 1

        for i in trange(nint, leave=False):
            data = get_fits_data(fname, return_header=False, bias=super_bias,
                                 reffix=True, DMS=DMS, int_ind=i, **kwargs)

            # Fit everything with linear first
            deg = 1
            cf_all = np.zeros([3,ny,nx])
            cf_all[:2] = jl_poly_fit(tarr[1:], data[1:,:,:], deg=deg)
            yfit = jl_poly(tarr, cf_all)
            dof = data.shape[0] - deg
            # Get reduced chi-sqr metric
            chired_poly = chisqr_red(data, yfit=yfit, dof=dof)

            # Fit polynomial to those not well fit by linear func
            chi_cutoff = 2
            ibad = (chired_poly > chi_cutoff) & (~mask_ref)
            deg = 2
            cf_all[:,ibad] = jl_poly_fit(tarr[1:], data[1:,ibad], deg=deg)
            yfit[:,ibad] = jl_poly(tarr, cf_all[:,ibad])
            dof = data.shape[0] - deg
            # Get reduced chi-sqr metric for poorly fit data
            chired_poly[ibad] = chisqr_red(data[:,ibad], yfit=yfit[:,ibad], dof=dof)
            
            del yfit

            # Find pixels poorly fit by any polynomial
            ibad = (chired_poly > chi_cutoff) & (~mask_ref)
            bias_off = cf_all[0]
            bias_off[ibad] = 0

            # Those active pixels well fit by a polynomial
            mask_poly = (chired_poly <= chi_cutoff) & (~mask_ref)

            # Pixels with large deviations (5-sigma outliers)
            med_diff = np.median(cf_all[1])*tarr.max()
            std_diff = robust.std(cf_all[1])*tarr.max()
            mask_deviant = (data[-1] - data[1]) > (med_diff + std_diff*5)

            # Pixels with negative slopes
            mask_negative = (data[-1] - data[1]) < -(med_diff + std_diff*5)

            # Others
            mask_others = (~mask_poly) & (~mask_ref) & (~mask_deviant) & (~mask_negative)
            
            # Save to masks lists
            masks_dict['mask_poly'].append(mask_poly)
            # masks_dict['mask_ref'].append(mask_ref)
            masks_dict['mask_deviant'].append(mask_deviant)
            masks_dict['mask_negative'].append(mask_negative)
            masks_dict['mask_others'].append(mask_others)

            # Fit slopes of weird pixels to get their y=0 (bias) offset
            # ifit_others = mask_deviant | mask_others | mask_negative
            ifit_others = (~mask_poly) & (~mask_ref)
            yvals_fit = data[0:15,ifit_others]
            dy = ramp_derivative(yvals_fit, fit0=True, deg=1, ifit=[0,10])
            yfit = np.cumsum(dy, axis=0)

            bias_off[ifit_others] = (yvals_fit[0] - yfit[0])
            bias_off_all.append(bias_off)        

            # Subtact bias
            data -= bias_off
            
            igood = mask_poly | mask_ref
            nsum[igood] += 1
            for j, im in enumerate(data):
                ramp_sum[j,igood] += im[igood]
                ramp_sum2[j] += im

            del data, yfit

    # Take averages
    igood = (nsum >= 0.75*nfiles)
    for im in ramp_sum:
        im[igood] /= nsum[igood]
    ramp_sum2 /= nfiles
    
    # Replace empty ramp_sum pixels with ramp_sum2
    # izero = np.sum(ramp_sum, axis=0) == 0
    ramp_sum[:,~igood] = ramp_sum2[:,~igood]
    ramp_avg = ramp_sum
    
    # del ramp_sum2
    
    # Get average of bias offsets
    bias_off_all = np.array(bias_off_all)
    bias_off_avg = robust.mean(bias_off_all, axis=0)
    
    # Convert masks to arrays
    for k in masks_dict.keys():
        masks_dict[k] = np.array(masks_dict[k])

    # Pixels with negative values
    mask_neg = (ramp_avg[0] < 0) & mask_act
    bias_off = np.zeros_like(bias_off_avg)

    yvals_fit = ramp_avg[:,mask_neg]
    dy = ramp_derivative(yvals_fit[0:15], fit0=True, deg=1, ifit=[0,10])
    yfit = np.cumsum(dy, axis=0)
    bias_off[mask_neg] = (yvals_fit[0] - yfit[0])

    # Pixels with largish positive values (indicative of RC pixels)
    mask_large = (ramp_avg[0] > 1000) | (ramp_avg[-1] > 50000)
    yvals_fit = ramp_avg[:,mask_large]
    dy = ramp_derivative(yvals_fit[0:15], fit0=True, deg=2, ifit=[0,10])
    yfit = np.cumsum(dy, axis=0)
    bias_off[mask_large] = (yvals_fit[0] - yfit[0])

    # Remove from ramp_avg and add into bias_off_avg
    ramp_avg -= bias_off
    bias_off_avg += bias_off

    # Pixels continuing to have largish positive values (indicative of RC pixels)
    bias_off = np.zeros_like(bias_off_avg)
    mask_large = ramp_avg[0] > 10000
    yvals_fit = ramp_avg[:,mask_large]
    dy = ramp_derivative(yvals_fit[0:15], fit0=False)
    yfit = np.cumsum(dy, axis=0)
    bias_off[mask_large] += (yvals_fit[0] - yfit[0])

    # Remove from ramp_avg and add into bias_off_avg
    ramp_avg -= bias_off
    bias_off_avg += bias_off

    pynrc.setup_logging(log_prev, verbose=False)

    return ramp_avg, bias_off_avg, masks_dict
    

def gen_col_variations(allfiles, super_bias=None, super_dark_ramp=None, 
    DMS=False, **kwargs):
    """ Create a series of column offset models 

    These are likely FETS in the ASIC preamp or ADC or detector
    column buffer jumping around and causing entire columns 
    within a ramp to transition between two states.

    Returns a series of ramp variations to add to entire columns
    as well as the probability a given column will be affected.

    """

    # Set logging to WARNING to suppress messages
    log_prev = pynrc.conf.logging_level
    pynrc.setup_logging('WARNING', verbose=False)

    hdr = fits.getheader(allfiles[0])
    det = create_detops(hdr, DMS=DMS)

    nchan = det.nout
    nx = det.xpix
    # ny = det.ypix
    # nz = det.multiaccum.ngroup
    chsize = det.chsize
    
    # nfiles = len(allfiles)
    
    if super_dark_ramp is None: 
        super_dark_ramp = 0
    if super_bias is None: 
        super_bias = 0
    
    ramp_column_varations = []
    nbad = []
    for f in tqdm(allfiles, desc='Files'):

        # If DMS, then might be multiple integrations per FITS file
        nint = fits.getheader(f)['NINTS'] if DMS else 1

        for i in trange(nint, leave=False, desc='Ramps'):
            # Subtract bias, but don't yet perform reffix
            data = get_fits_data(f, bias=super_bias, DMS=DMS, int_ind=i)

            # Subtract super_dark_ramp to get residuals
            data -= super_dark_ramp

            data = reffix_hxrg(data, **kwargs)

            # Take the median of each column
            data_ymed = np.median(data, axis=1)
            # Set each ramp residual to 0 offset
            data_ymed -= np.median(data_ymed, axis=0)
            # Get rid of residual channel offsets
            for ch in range(nchan):
                x1 = ch*chsize
                x2 = x1 + chsize
                data_ymed[:,x1:x2] -= np.median(data_ymed[:,x1:x2], axis=1).reshape([-1,1])

            del data

            # Get derivatives
            # dymed = ramp_derivative(data_ymed, fit0=False)

            # Determine which columns have large excursions
            ymed_avg = np.mean(data_ymed, axis=0)
            ymed_std = np.std(data_ymed, axis=0)

            # dymed_avg = np.mean(dymed, axis=0)
            # dymed_std = np.std(dymed, axis=0)
            # print(np.median(ymed_avg), np.median(ymed_std))
            # print(robust.std(ymed_avg), robust.std(ymed_std))

            # Mask of outliers
            mask_outliers1 = np.abs(ymed_avg) > np.median(ymed_avg) + 1*robust.std(ymed_avg)
            mask_outliers2 = ymed_std > np.median(ymed_std) + 1*robust.std(ymed_std)
            mask_outliers = mask_outliers1 | mask_outliers2
            mask_outliers[:4] = False
            mask_outliers[-4:] = False

            data_ymed_outliers = data_ymed[:,mask_outliers]
            # dymed_outliers = dymed[:,mask_outliers]

            # data_ymed_good = data_ymed[:,~mask_outliers]
            # dymed_good = dymed[:,~mask_outliers]

            ramp_column_varations.append(data_ymed_outliers)
            nbad.append(data_ymed_outliers.shape[1])

    ramp_column_varations = np.hstack(ramp_column_varations)

    nbad = np.array(nbad)
    prob_bad = np.mean(nbad/nx)

    pynrc.setup_logging(log_prev, verbose=False)

    return ramp_column_varations, prob_bad


# Main reference bias offsets
# Amplifier bias offsets
def get_bias_offsets(data, nchan=4, ref_bot=True, ref_top=True):
    """ Get Reference Bias Characteristics

    Given some ramp data, determine the average master bias offset
    as well as the relative individual amplifier offsets. Also
    return the frame-to-frame variations caused by the preamp
    resets.
    """
    
    if ref_bot==False and ref_top==False:
        print('Need top and/or bottom refernece to be True')
        return

    nz, ny, nx = data.shape
    chsize = int(nx/nchan)
    
    # Mask of top and bottom reference pixels
    mask_ref = np.zeros([ny,nx]).astype('bool')
    mask_ref[0:4,:] = ref_bot
    mask_ref[-4:,:] = ref_top

    # Reference offsets for each frame
    bias_off_frame = np.median(data[:,mask_ref], axis=1)
    bias_mn = np.mean(bias_off_frame)
    bias_std_f2f = robust.std(bias_off_frame)
    
    # Remove average bias offsets from each frame
    for i, im in enumerate(data):
        im -= bias_off_frame[i]
    
    # Determine amplifier offsets
    amp_mn_all = []
    amp_std_f2f_all = []
    for ch in range(nchan):
        mask_ch = np.zeros([ny,nx]).astype('bool')
        mask_ch[:,ch*chsize:(ch+1)*chsize] = True
        
        mask_ch_pix = mask_ref & mask_ch
        
        # Reference pixel offsets for this amplifier
        data_ch = data[:,mask_ch_pix]

        amp_off_frame = np.median(data_ch, axis=1)
        amp_mn = np.mean(amp_off_frame)
        amp_std_f2f = robust.std(amp_off_frame)
        
        amp_mn_all.append(amp_mn)
        amp_std_f2f_all.append(amp_std_f2f)
        
    amp_mn_all = np.array(amp_mn_all)
    amp_std_f2f_all = np.array(amp_std_f2f_all)

    return bias_mn, bias_std_f2f, amp_mn_all, amp_std_f2f_all

def get_oddeven_offsets(data, nchan=4, ref_top=True, ref_bot=True, bias_off=None, amp_off=None):
    """ Even/Odd Column Offsets

    Return the per-amplifier offsets of the even and odd
    columns relative after subtraction of the matster and
    amplifier bias offsets.
    """
    
    if bias_off is None:
        bias_off = 0
    if amp_off is None:
        amp_off = np.zeros(nchan)
    
    nz, ny, nx = data.shape
    chsize = int(nx / nchan)
    
    mask_ref_even = np.zeros([ny,nx]).astype('bool')
    mask_ref_even[0:4,0::2] = ref_bot
    mask_ref_even[-4:,0::2] = ref_top

    mask_ref_odd = np.zeros([ny,nx]).astype('bool')
    mask_ref_odd[0:4,1::2] = ref_bot
    mask_ref_odd[-4:,1::2] = ref_top

    ch_odd_vals_ref = []
    ch_even_vals_ref = []
    for ch in range(nchan):

        # Reference pixels
        mask_ch = np.zeros([ny,nx]).astype('bool')
        mask_ch[:,ch*chsize:(ch+1)*chsize] = True
        mask_even_ch = mask_ch & mask_ref_even
        mask_odd_ch = mask_ch & mask_ref_odd

        data_ref_even = data[:,mask_even_ch]
        data_ref_odd = data[:,mask_odd_ch]
        
        data_ref_even_offset = np.mean(data_ref_even) - bias_off - amp_off[ch]
        data_ref_odd_offset = np.mean(data_ref_odd) - bias_off - amp_off[ch]

        ch_odd_vals_ref.append(data_ref_odd_offset)
        ch_even_vals_ref.append(data_ref_even_offset)

    ch_odd_vals_ref = np.array(ch_odd_vals_ref)
    ch_even_vals_ref = np.array(ch_even_vals_ref)
    
    return ch_even_vals_ref, ch_odd_vals_ref

def get_ref_instability(data, nchan=4, mn_func=np.median):
    """ Reference Pixel Instability

    Determine the instability of the average reference pixel
    values relative to the active pixels on a frame-to-frame
    basis. The procedure is to compute a series of CDS frames,
    then look at the peak distributions of the active pixels
    relative to the reference pixels.
    """
    
    cds = data[1:] - data[:-1]
    nz, ny, nx = data.shape
    
    chsize = int(nx / nchan)

    # Mask of active pixels
    mask_act = np.zeros([ny,nx]).astype('bool')
    mask_act[4:-4,4:-4] = True

    # Mask of top and bottom reference pixels
    mask_ref = np.zeros([ny,nx]).astype('bool')
    mask_ref[0:4,:] = True
    mask_ref[-4:,:] = True
    
    ref_inst = []
    for ch in range(nchan):
        mask_ch = np.zeros([ny,nx]).astype('bool')
        mask_ch[:,ch*chsize:(ch+1)*chsize] = True

        cds_ref = cds[:,mask_ref & mask_ch]
        cds_act = cds[:,mask_act & mask_ch]

        cds_ref_mn = mn_func(cds_ref, axis=1)
        cds_act_mn = mn_func(cds_act, axis=1)

        # Relative to Reference
        cds_act_mn -= cds_ref_mn

        ref_inst.append(np.std(cds_act_mn) / np.sqrt(2))

    ref_inst = np.array(ref_inst)
    
    return ref_inst

def gen_ref_dict(allfiles, super_bias, super_dark_ramp=None, DMS=False, **kwargs):
    """ Generate Reference Pixel Behavior Dictionary
    """

    if super_dark_ramp is None:
        super_dark_ramp = 0
        
    # nfiles = len(allfiles)

    # Header info from first file
    hdr = fits.getheader(allfiles[0])
    det = create_detops(hdr, DMS=DMS)
    nchan = det.nout
    
    bias_mn_ref_all = []      # Main bias average offset
    bias_std_f2f_ref_all = [] # Main bias standard deviation per int
    amp_mn_ref_all = []       # Amplifier ref offset per integration
    amp_std_f2f_ref_all = []  # Ampl Ref frame-to-frame variations

    # Even/Odd Column Offsets
    col_even_offset_ref = []
    col_odd_offset_ref = []

    # Ref Instability frame-to-frame
    amp_std_ref_act_all = [] 
    for fname in tqdm(allfiles, desc='Files'):

        # If DMS, then might be multiple integrations per FITS file
        nint = fits.getheader(fname)['NINTS'] if DMS else 1

        for i in trange(nint, leave=False, desc='Ramps'):
            # Relative to super bias and super dark ramp
            data = get_fits_data(fname, bias=super_bias, DMS=DMS, int_ind=i)

            data -= super_dark_ramp

            # Get master and amplifer offsets
            res = get_bias_offsets(data, nchan=nchan)

            bias_mn_ref_all.append(res[0])
            bias_std_f2f_ref_all.append(res[1])
            amp_mn_ref_all.append(res[2])
            amp_std_f2f_ref_all.append(res[3])

            # bias_off was subtracted in-place from data within get_bias_offsets()
            res_col = get_oddeven_offsets(data, nchan=nchan, bias_off=0, amp_off=res[2])
            col_even_offset_ref.append(res_col[0])
            col_odd_offset_ref.append(res_col[1])

            # Reference pixel instabilities
            data = reffix_hxrg(data, **kwargs)
            ref_inst = get_ref_instability(data, nchan=nchan)
            amp_std_ref_act_all.append(ref_inst)

            del data

    bias_mn_ref_all      = np.array(bias_mn_ref_all)
    bias_std_f2f_ref_all = np.array(bias_std_f2f_ref_all)
    amp_mn_ref_all       = np.array(amp_mn_ref_all)
    amp_std_f2f_ref_all  = np.array(amp_std_f2f_ref_all)

    col_even_offset_ref = np.array(col_even_offset_ref)
    col_odd_offset_ref  = np.array(col_odd_offset_ref)

    amp_std_ref_act_all = np.array(amp_std_ref_act_all)
    
    
    ref_dict = {}

    # Master bias offsets
    ref_dict['master_bias_mean'] = bias_mn_ref_all.mean()
    ref_dict['master_bias_std'] = robust.medabsdev(bias_mn_ref_all)
    ref_dict['master_bias_f2f'] = np.sqrt(np.mean(bias_std_f2f_ref_all**2))

    # Amplifier Offsets
    ref_dict['amp_offset_mean'] = amp_mn_ref_all.mean(axis=0)
    # There can be correlations between offsets that depend on temperature
    # Let's remove those to get the true standard deviation
    cf = jl_poly_fit(bias_mn_ref_all, amp_mn_ref_all)
    amp_sub = amp_mn_ref_all - jl_poly(bias_mn_ref_all, cf)
    ref_dict['amp_offset_std'] = robust.std(amp_sub, axis=0)
    ref_dict['amp_offset_f2f'] = np.sqrt(np.mean(amp_std_f2f_ref_all**2, axis=0))

    # Correlation between master_bias_mean and amp_offset_mean
    ref_dict['master_amp_cf'] = cf

    # Even/Odd Column offsets
    ref_dict['amp_even_col_offset'] = (np.mean(col_even_offset_ref, axis=0))
    ref_dict['amp_odd_col_offset']  = (np.mean(col_odd_offset_ref, axis=0))

    # Reference instability relative active pixels
    ref_dict['amp_ref_inst_f2f'] = np.sqrt(np.mean(amp_std_ref_act_all**2, axis=0))

    _log.info("Reference Pixels")

    _log.info('')
    _log.info("Master Bias Mean")
    _log.info(ref_dict['master_bias_mean'])
    _log.info("Master Bias StDev")
    _log.info(ref_dict['master_bias_std'])
    _log.info("Master Bias Frame-to-Frame StDev")
    _log.info(ref_dict['master_bias_f2f'])

    _log.info('')
    _log.info("Amp Offset Mean")
    _log.info(ref_dict['amp_offset_mean'])
    _log.info("Amp Offset StDev")
    _log.info(ref_dict['amp_offset_std'])
    _log.info("Amp Offset Frame-to-Frame StDev")
    _log.info(ref_dict['amp_offset_f2f'])

    _log.info("")
    _log.info("Even Columns Offset")
    _log.info(ref_dict['amp_even_col_offset'])
    _log.info("Odd Columns Offset")
    _log.info(ref_dict['amp_odd_col_offset'])

    _log.info("")
    _log.info("Reference Instability")
    _log.info(ref_dict['amp_ref_inst_f2f'])
    
    return ref_dict

def calc_cdsnoise(data, temporal=True, spatial=True, std_func=np.std):
    """ Calculate CDS noise from input image cube"""

    if (temporal==False) and (spatial==False):
        _log.warn("Must select one or both of `temporal` or `spatial`")
        return
    
    # Make sure we select same number of even/odd frame
    vals1 = data[0::2]
    vals2 = data[1::2]
    nz1 = vals1.shape[0]
    nz2 = vals2.shape[0]
    nz = np.min([nz1,nz2])
    
    # Calculate CDS image pairs
    cds_arr = vals2[:nz] - vals1[:nz]
    
    # CDS noise per pixel (temporal)
    if temporal:
        cds_temp = std_func(cds_arr, axis=0)
        # Take median of the variance
        cds_temp_med = np.sqrt(np.median(cds_temp**2))
        
    # CDS noise per frame (spatial)
    if spatial:
        sh = cds_arr.shape
        cds_spat = std_func(cds_arr.reshape([sh[0],-1]), axis=1)
        # Take median of the variance
        cds_spat_med = np.sqrt(np.median(cds_spat**2))
        
    if temporal and spatial:
        res = cds_temp_med, cds_spat_med
    elif temporal:
        res = cds_temp_med
    elif spatial:
        res = cds_spat_med
    
    return res


def gen_cds_dict(allfiles, DMS=False, superbias=None,
                 mask_good_arr=None, same_scan_direction=False):
    """ Generate dictionary of CDS noise info
    
    Calculate read noise for:
      1. Total noise (no column correcton)
      2. 1/f noise (no column correcton)
      3. Intrinsic read noise (w/ column correcton)
      4. Both temporal and spatial
    
    """

    # Header info from first file
    hdr = fits.getheader(allfiles[0])
    det = create_detops(hdr, DMS=DMS)
    
    nchan = det.nout
    nx = det.xpix
    ny = det.ypix
    nz = det.multiaccum.ngroup
    chsize = det.chsize

    # nfiles = len(allfiles)

    cds_act_dict = {
        'spat_tot': [], 'spat_det': [], 
        'temp_tot': [], 'temp_det': [], 
        'spat_pink_corr': [], 'spat_pink_uncorr': [],
        'temp_pink_corr': [], 'temp_pink_uncorr': [],
    }

    cds_ref_dict = {
        'spat_tot': [], 'spat_det': [],
        'temp_tot': [], 'temp_det': [],
    }
    
    # Active and reference pixel masks
    lower, upper, left, right = det.ref_info

    # Reference pixel mask
    # Just use top and bottom ref pixel
    mask_ref = np.zeros([ny,nx], dtype='bool')
    mask_ref[0:lower,:] = True
    mask_ref[-upper:,:] = True
    
    # Active pixels mask
    mask_act = np.zeros([ny,nx], dtype='bool')
    mask_act[lower:-upper,left:-right] = True
    
    # Channel mask
    mask_channels = np.zeros([ny,nx])
    for ch in range(nchan):
        mask_channels[:,ch*chsize:(ch+1)*chsize] = ch
        
    # Mask of good pixels
    if mask_good_arr is None:
        mask_good_arr = np.ones([nz,ny,nx], dtype='bool')

    kwargs = {
        'nchans': nchan, 'altcol': True, 'in_place': True,    
        'fixcol': False, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
    }

    for fname in tqdm(allfiles, desc='Files'):

        # If DMS, then might be multiple integrations per FITS file
        nint = fits.getheader(fname)['NINTS'] if DMS else 1

        for i in trange(nint, leave=False, desc='Ramps'):
            # Relative to super bias and super dark ramp
            data = get_fits_data(fname, bias=superbias, DMS=DMS, int_ind=i)

            ##################################
            # 1. Full noise (det + 1/f)
            kwargs['fixcol'] = False
            data = get_fits_data(fname, bias=superbias, reffix=True, 
                                 DMS=DMS, int_ind=i, **kwargs)

            # Active pixels in each channel
            cds_temp_arr = []
            cds_spat_arr = []
            indgood = (mask_good_arr[i]) & mask_act
            for ch in np.arange(nchan):
                ind = indgood & (mask_channels == ch)
                cds_temp, cds_spat = calc_cdsnoise(data[:,ind])
                cds_temp_arr.append(cds_temp)
                cds_spat_arr.append(cds_spat)
            cds_act_dict['temp_tot'].append(cds_temp_arr)
            cds_act_dict['spat_tot'].append(cds_spat_arr)

            # Reference pixels in each channel
            cds_temp_arr = []
            cds_spat_arr = []
            indgood = mask_ref
            for ch in np.arange(nchan):
                ind = indgood & (mask_channels == ch)
                cds_temp, cds_spat = calc_cdsnoise(data[:,ind])
                cds_temp_arr.append(cds_temp)
                cds_spat_arr.append(cds_spat)
            cds_ref_dict['temp_tot'].append(cds_temp_arr)
            cds_ref_dict['spat_tot'].append(cds_spat_arr)

            ##################################
            # 2. 1/f noise contributions

            # Create array of extracted 1/f noise
            # Work on CDS pairs
            fn_data = []
            cds_data = data[1:20:2] - data[0:20:2]
            for im in cds_data:
                ch_arr = im.reshape([ny,-1,chsize]).transpose([1,0,2])
                mask = np.abs(im - np.median(im)) > 10*robust.medabsdev(im)
                mask = mask.reshape([ny,-1,chsize]).transpose([1,0,2])
                fnoise = channel_smooth_savgol(ch_arr, mask=mask)
                fnoise = fnoise.transpose([1,0,2]).reshape([ny,nx])
                fn_data.append(fnoise)
            fn_data = np.array(fn_data)
            # Divide by sqrt(2) since we've already performed a CDS difference
            fn_data /= np.sqrt(2)

            # Split into correlated and uncorrelated components
            fn_data_corr = []
            for j, im in enumerate(fn_data):
                fn_corr = channel_averaging(im, nchans=nchan,  off_chans=False,
                    same_scan_direction=same_scan_direction, mn_func=np.mean)
                # Subtract from fn_data
                fn_data[j] -= fn_corr
                # Only append first channel since the rest are the same data
                fn_data_corr.append(fn_corr[:,0:chsize])
            fn_data_corr = np.array(fn_data_corr)
            
            # Active pixels noise in each channel for uncorrelated data
            cds_temp_arr = []
            cds_spat_arr = []
            indgood = (mask_good_arr[i]) & mask_act
            for ch in np.arange(nchan):
                ind = indgood & (mask_channels == ch)
                cds_temp, cds_spat = calc_cdsnoise(fn_data[:,ind])
                cds_temp_arr.append(cds_temp)
                cds_spat_arr.append(cds_spat)
            cds_act_dict['temp_pink_uncorr'].append(cds_temp_arr)
            cds_act_dict['spat_pink_uncorr'].append(cds_spat_arr)

            del fn_data

            # Active pixels noise in correlated channel data
            indgood = (mask_good_arr[i]) & mask_act
            ind = indgood[:,0:chsize]
            cds_temp, cds_spat = calc_cdsnoise(fn_data_corr[:,ind])
            cds_act_dict['temp_pink_corr'].append(cds_temp)
            cds_act_dict['spat_pink_corr'].append(cds_spat)

            del fn_data_corr

            ##################################
            # 3. Detector contributions
            kwargs['fixcol'] = True
            data = reffix_hxrg(data, **kwargs)

            # New 1/f noise array
            for j, im in enumerate(data):
                ch_arr = im.reshape([ny,-1,chsize]).transpose([1,0,2])
                fnoise = channel_smooth_savgol(ch_arr)
                fnoise = fnoise.transpose([1,0,2]).reshape([ny,nx])
                # Remove 1/f noise contributions
                data[j] -= fnoise

            # Active pixels in each channel
            cds_temp_arr = []
            cds_spat_arr = []
            indgood = (mask_good_arr[i]) & mask_act
            for ch in np.arange(nchan):
                ind = indgood & (mask_channels == ch)
                cds_temp, cds_spat = calc_cdsnoise(data[:,ind])
                cds_temp_arr.append(cds_temp)
                cds_spat_arr.append(cds_spat)
            cds_act_dict['temp_det'].append(cds_temp_arr)
            cds_act_dict['spat_det'].append(cds_spat_arr)

            # Reference pixels in each channel
            cds_temp_arr = []
            cds_spat_arr = []
            indgood = mask_ref
            for ch in np.arange(nchan):
                ind = indgood & (mask_channels == ch)
                cds_temp, cds_spat = calc_cdsnoise(data[:,ind])
                cds_temp_arr.append(cds_temp)
                cds_spat_arr.append(cds_spat)
            cds_ref_dict['temp_det'].append(cds_temp_arr)
            cds_ref_dict['spat_det'].append(cds_spat_arr)

            # Done with data
            del data

    # Convert lists to np.array
    dlist = [cds_act_dict, cds_ref_dict]
    for d in dlist:
        for k in d.keys():
            if isinstance(d[k], (list)):
                d[k] = np.array(d[k])

    return cds_act_dict, cds_ref_dict


def ramp_resample(data, det_new, return_zero_frame=False):
    """ Resample a RAPID dataset into new detector format"""
    
    nz, ny, nx = data.shape
    
    x1, y1 = (det_new.x0, det_new.y0)
    xpix, ypix = (det_new.xpix, det_new.ypix)
    x2 = x1 + xpix
    y2 = y1 + ypix
    
    
    ma  = det_new.multiaccum
    nd1     = ma.nd1
    nd2     = ma.nd2
    nf      = ma.nf
    ngroup  = ma.ngroup        

    # Number of total frames up the ramp (including drops)
    # Keep last nd2 for reshaping
    nread_tot = nd1 + ngroup*nf + (ngroup-1)*nd2
    
    assert nread_tot <= nz, f"Output ramp has more total read frames ({nread_tot}) than input ({nz})."

    # Crop dataset
    data_out = data[0:nread_tot, y1:y2, x1:x2]

    # Save the first frame (so-called ZERO frame) for the zero frame extension
    if return_zero_frame:
        zeroData = deepcopy(data_out[0])
        
    # Remove drops and average grouped data
    if nf>1 or nd2>0:
        # Trailing drop frames were already excluded, so need to pull off last group of avg'ed frames
        data_end = data_out[-nf:,:,:].mean(axis=0) if nf>1 else data[-1:,:,:]
        data_end = data_end.reshape([1,ypix,xpix])
        
        # Only care about first (n-1) groups for now
        # Last group is handled separately
        data_out = data_out[:-nf,:,:]

        # Reshape for easy group manipulation
        data_out = data_out.reshape([-1,nf+nd2,ypix,xpix])
        
        # Trim off the dropped frames (nd2)
        if nd2>0: 
            data_out = data_out[:,:nf,:,:]

        # Average the frames within groups
        # In reality, the 16-bit data is bit-shifted
        data_out = data_out.reshape([-1,ypix,xpix]) if nf==1 else data_out.mean(axis=1)

        # Add back the last group (already averaged)
        data_out = np.append(data_out, data_end, axis=0)

    if return_zero_frame:
        return data_out, zeroData
    else:
        return data_out

def calc_eff_noise(allfiles, superbias=None, temporal=True, spatial=True, 
                   ng_all=None, DMS=False, kw_ref=None, std_func=robust.medabsdev,
                   kernel_ipc=None, kernel_ppc=None, read_pattern='RAPID'):
    """ Determine Effective Noise

    Calculates the slope noise (in DN/sec) assuming a linear fits to a variety
    number of groups. The idea is to visualize the reduction in noise as you
    increase the number of groups in the fit and compare it to theoretical
    predictions (ie., slope noise formula).
    
    Parameters
    ----------
    allfiles : list
        List of input file names.
    DMS : bool
        Are files DMS formatted?
    superbias : ndarray
        Super bias to subtract from each dataset.
    temporal : bool
        Calculate slope noise using pixels' temporal distribution?
    spatial : bool
        Calcualte slope noise using pixel spatial distribution?
    ng_all : array-like
        Array of group to perform linear fits for slope calculations.
    kw_ref : dict
        Dictionary of keywords to pass to reference correction routine.
    std_func : func
        Function for calculating spatial distribution.
    kernel_ipc : ndarray
        IPC kernel to perform deconvolution on slope images.
    kernel_ppc : ndarray
        Similar to `kernel_ipc` except for PPC.
    read_pattern : string
        Reformulate data as if it were acquired using a read pattern
        other than RAPID.
    """

    log_prev = pynrc.conf.logging_level
    pynrc.setup_logging('WARNING', verbose=False)

    hdr = fits.getheader(allfiles[0])
    det = create_detops(hdr, DMS=DMS)
    
    nchan = det.nout
    nx = det.xpix
    ny = det.ypix
    chsize = det.chsize

    # Masks for active, reference, and amplifiers
    rlow, rup, rleft, rright = det.ref_info
    act_mask = np.zeros([ny,nx]).astype('bool')
    act_mask[rlow:-rup,rleft:-rright] = True
    ref_mask = ~act_mask
    ch_mask = np.zeros([ny,nx])
    for ch in np.arange(nchan):
        ch_mask[:,ch*chsize:(ch+1)*chsize] = ch
        
    if 'RAPID' not in read_pattern:
        det_new = deepcopy(det)
        ma_new = det_new.multiaccum
        # Change read mode and determine max number of allowed groups
        ma_new.read_mode = read_pattern
        ma_new.ngroup = int((det.multiaccum.ngroup - ma_new.nd1 + ma_new.nd2) / (ma_new.nf + ma_new.nd2))

        nz = ma_new.ngroup
        # Group time
        # tarr = np.arange(1, nz+1) * det_new.time_group + (ma_new.nd1 - ma_new.nd2 - ma_new.nf/2)*det_new.time_frame
        tarr = det_new.times_group_avg
        # Select number of groups to perform linear fits
        if ng_all is None:
            if nz<20:
                ng_all = np.arange(2,nz+1).astype('int')
            else:
                ng_all = np.append([2,3], np.linspace(5,nz,num=16).astype('int'))
    else:
        nz = det.multiaccum.ngroup
        # Group time
        tarr = np.arange(1, nz+1) * det.time_group
        # Select number of groups to perform linear fits
        if ng_all is None:
            ng_all = np.append([2,3,5], np.linspace(10,nz,num=15).astype('int'))

    # Make sure ng_all is unique
    ng_all = np.unique(ng_all)

    # Do not remove 1/f noise via ref column
    if kw_ref is None:
        kw_ref = {
            'nchans': nchan, 'altcol': True, 'in_place': True,    
            'fixcol': False, 'avg_type': 'pixel', 'savgol': True, 'perint': False    
        }
        
    # IPC and PPC kernels
    if kernel_ipc is not None:
        ipc_big = pad_or_cut_to_size(kernel_ipc, (ny,nx))
        kipc_fft = np.fft.fft2(ipc_big)
    else:
        kipc_fft = None
    if kernel_ppc is not None:
        ppc_big = pad_or_cut_to_size(kernel_ppc, (ny,chsize))
        kppc_fft = np.fft.fft2(ppc_big)
    else:
        kppc_fft = None

    # nfiles = len(allfiles)

    # Calculate effective noise temporally
    if temporal:

        eff_noise_temp = []
        # Work with one channel at a time for better memory management
        for ch in trange(nchan, desc="Temporal", leave=False):
            ind_ch = act_mask & (ch_mask==ch)

            slope_chan_allfiles = []
            slope_ref_allfiles = []
            for fname in tqdm(allfiles, leave=False, desc="Files"):
                # If DMS, then might be multiple integrations per FITS file
                nint = fits.getheader(fname)['NINTS'] if DMS else 1
                for i in trange(nint, leave=False, desc='Ramps'):
                    data = get_fits_data(fname, bias=superbias, reffix=True, 
                                         DMS=DMS, int_ind=i, **kw_ref)

                    # Reformat data?
                    if 'RAPID' not in read_pattern:
                        data = ramp_resample(data, det_new)
                    
                    slope_chan = []
                    slope_ref = []
                    for fnum in tqdm(ng_all, leave=False, desc="Group Fit"):
                        bias, slope = jl_poly_fit(tarr[0:fnum], data[0:fnum])
                        
                        # Deconvolve fits to remove IPC and PPC
                        if kipc_fft is not None:
                            slope = ipc_deconvolve(slope, None, kfft=kipc_fft)
                        if kppc_fft is not None:
                            slope = ppc_deconvolve(slope, None, kfft=kppc_fft, in_place=True)

                        slope_chan.append(slope[ind_ch])

                        # Do reference pixels
                        if ch==nchan-1:
                            slope_ref.append(slope[ref_mask])

                    slope_chan_allfiles.append(np.array(slope_chan))
                    if ch==nchan-1:
                        slope_ref_allfiles.append(np.array(slope_ref))

                    del data

            slope_chan_allfiles = np.array(slope_chan_allfiles)
            # Reference pixels
            if ch==nchan-1:
                slope_ref_allfiles = np.array(slope_ref_allfiles)

            # Calculate std dev for each pixels
            std_pix = np.std(slope_chan_allfiles, axis=0)
            # Get the median of the variance distribution
            eff_noise = np.sqrt(np.median(std_pix**2, axis=1))
            eff_noise_temp.append(eff_noise)
            if ch==nchan-1:
                std_pix = np.std(slope_ref_allfiles, axis=0)
                eff_noise_ref = np.sqrt(np.median(std_pix**2, axis=1))
                eff_noise_temp.append(eff_noise_ref)
            del slope_chan, slope_chan_allfiles, std_pix

        eff_noise_temp = np.array(eff_noise_temp)

    # Calculate effective noise spatially
    if spatial:
        eff_noise_all = []
        for f in tqdm(allfiles, desc="Spatial", leave=False):
            # If DMS, then might be multiple integrations per FITS file
            nint = fits.getheader(fname)['NINTS'] if DMS else 1
            for i in trange(nint, leave=False, desc='Ramps'):
                data = get_fits_data(f, bias=superbias, reffix=True, 
                                     DMS=DMS, ind_int=i, **kw_ref)

                # Reformat data?
                if 'RAPID' not in read_pattern:
                    data = ramp_resample(data, det_new)

                eff_noise_chans = []
                # Spatial standard deviation
                for fnum in tqdm(ng_all, leave=False, desc="Group Fit"):
                    bias, slope = jl_poly_fit(tarr[0:fnum], data[0:fnum])

                    # Deconvolve fits to remove IPC and PPC
                    if kipc_fft is not None:
                        slope = ipc_deconvolve(slope, None, kfft=kipc_fft)
                    if kppc_fft is not None:
                        slope = ppc_deconvolve(slope, None, kfft=kppc_fft, in_place=True)
                    
                    eff_noise = []
                    # Each channel
                    for ch in np.arange(nchan):
                        ind_ch = act_mask & (ch_mask==ch)
                        eff_noise.append(std_func(slope[ind_ch]))
                    # Add reference pixels
                    eff_noise.append(std_func(slope[ref_mask]))

                    # Append to final array
                    eff_noise_chans.append(np.array(eff_noise))

                eff_noise_chans = np.array(eff_noise_chans).transpose()
                eff_noise_all.append(eff_noise_chans)

                del data

        eff_noise_all = np.array(eff_noise_all)
        eff_noise_spat = np.median(eff_noise_all, axis=0)

    pynrc.setup_logging(log_prev, verbose=False)
        
    if temporal and spatial:
        res = ng_all, eff_noise_temp, eff_noise_spat
    elif temporal:
        res = ng_all, eff_noise_temp
    elif spatial:
        res = ng_all, eff_noise_spat
    
    return res

from pynrc.nrc_utils import var_ex_model
def fit_func_var_ex(params, det, patterns, ng_all_list, en_dn_list, 
    read_noise=None, idark=None, ideal_Poisson=False):
    """Function for lsq fit to get excess variance"""
    
    gain = det.gain
    if idark is None:
        idark = det.dark_current
    
    # Read noise per frame
    if read_noise is None:
        cds_var = (en_dn_list[0][0] * det.time_group * gain)**2 - (idark * det.time_frame)
        read_noise = np.sqrt(cds_var / 2)
    
    diff_all = []
    for i, patt in enumerate(patterns):

        det_new = deepcopy(det)
        ma_new = det_new.multiaccum
        ma_new.read_mode = patt
        ma_new.ngroup = int((det.multiaccum.ngroup - ma_new.nd1 + ma_new.nd2) / (ma_new.nf + ma_new.nd2))

        ng_all = ng_all_list[i]
        thr_e = det_new.pixel_noise(ng=ng_all, rn=read_noise, idark=idark, 
                                    ideal_Poisson=ideal_Poisson, p_excess=[0,0])
        
        tvals = (ng_all - 1) * det_new.time_group
        var_ex_obs = (en_dn_list[i] * gain * tvals)**2 - (thr_e * tvals)**2

        nf = ma_new.nf
        var_ex_fit = var_ex_model(ng_all, nf, params)

        diff_all.append(var_ex_obs - var_ex_fit)
        
    return np.concatenate(diff_all)

def ipc_deconvolve(im, kernel, kfft=None):
    """Simple IPC image deconvolution
    
    Given an image (or image cube), apply an IPC deconvolution kernel
    to obtain the intrinsic flux distribution. Should also work for 
    PPC kernels. This simply calculates the FFT of the image(s) and
    kernel, divides them, then applies an iFFT to determine the
    deconvolved image.
    
    If performing PPC deconvolution, make sure to perform channel-by-channel
    with the kernel in the appropriate scan direction. IPC is usually symmetric,
    so this restriction may not apply.
 
    Parameters
    ==========
    im : ndarray
        Image or array of images. 
    kernel : ndarry
        Deconvolution kernel. Ignored if `kfft` is specified.
    kfft : Complex ndarray
        Option to directy supply the kernel's FFT rather than
        calculating it within the function. The supplied ndarray
        should have shape (ny,nx) equal to the input `im`. Useful
        if calling ``ipc_deconvolve`` multiple times.
    """

    # bias the image to avoid negative pixel values in image
    min_im = np.min(im)
    im = im - min_im

    # FFT of input image
    imfft = np.fft.fft2(im)

    # FFT of kernel
    if kfft is None:
        ipc_big = pad_or_cut_to_size(kernel, (im.shape[-2],im.shape[-1]))
        kfft = np.fft.fft2(ipc_big)

    im_final = np.fft.fftshift(np.fft.ifft2(imfft/kfft).real, axes=(-2,-1)) + min_im

    return im_final

def ppc_deconvolve(im, kernel, kfft=None, nchans=4, in_place=False,
    same_scan_direction=False, reverse_scan_direction=False):
    """PPC image deconvolution
    
    Given an image (or image cube), apply PPC deconvolution kernel
    to obtain the intrinsic flux distribution. 
    
    If performing PPC deconvolution, make sure to perform channel-by-channel
    with the kernel in the appropriate scan direction. IPC is usually symmetric,
    so this restriction may not apply.
 
    Parameters
    ==========
    im : ndarray
        Image or array of images. 
    kernel : ndarry
        Deconvolution kernel. Ignored if `kfft` is specified.
    kfft : Complex ndarray
        Option to directy supply the kernel's FFT rather than
        calculating it within the function. The supplied ndarray
        should have shape (ny,nx) equal to the input `im`. Useful
        if calling ``ppc_deconvolve`` multiple times.
    """

    # Need copy, otherwise will overwrite input data 
    if not in_place:
        im = im.copy()

    # Image cube shape
    sh = im.shape
    ndim = len(sh)
    if ndim==2:
        ny, nx = sh
        nz = 1
    else:
        nz, ny, nx = sh
    chsize = int(nx / nchans)
    im = im.reshape([nz,ny,nchans,-1])

    # FFT of kernel
    if kfft is None:
        k_big = pad_or_cut_to_size(kernel, (ny,chsize))
        kfft = np.fft.fft2(k_big)

    # Channel-by-channel deconvolution
    for ch in np.arange(nchans):
        sub = im[:,:,ch,:]
        if same_scan_direction:
            flip = True if reverse_scan_direction else False
        elif np.mod(ch,2)==0:
            flip = True if reverse_scan_direction else False
        else:
            flip = False if reverse_scan_direction else True

        if flip: 
            sub = sub[:,:,::-1]

        sub = ipc_deconvolve(sub, kernel, kfft=kfft)
        if flip: 
            sub = sub[:,:,::-1]
        im[:,:,ch,:] = sub

    im = im.reshape(sh)

    return im


def get_ipc_kernel(imdark, tint=None, boxsize=5, nchans=4, bg_remove=True,
                   hotcut=[5000,50000], calc_ppc=False,
                   same_scan_direction=False, reverse_scan_direction=False,
                   suppress_error_msg=False):
    """ Derive IPC/PPC Convolution Kernels
    
    Find the IPC and PPC kernels used to convolve detector pixel data.
    Finds all hot pixels within hotcut parameters and measures the
    average relative flux within adjacent pixels.

    Parameters
    ==========
    imdark : ndarray
        Image to search for hot pixels in units of DN or DN/sec. 
        If in terms of DN/sec, make sure to set `tint` to convert to raw DN.

    Keyword Parameters
    ==================
    tint : float or None
        Integration time to convert dark current rate into raw pixel values (DN).
        If None, then input image is assumed to be in units of DN.
    boxsize : int
        Size of the box. Should be odd. If even, will increment by 1.
    nchans : int
        Number of amplifier channels; necessary for PPC measurements. 
    bg_remove : bool
        Remove the average dark current values for each hot pixel cut-out.
        Only works if boxsize>3.
    hotcut : array-like
        Min and max values of hot pixels (above bg and bias) to cosider.
    calc_ppc : bool
        Calculate and return post-pixel coupling?
    same_scan_direction : bool
        Are all the output channels read in the same direction?
        By default fast-scan readout direction is ``[-->,<--,-->,<--]``
        If ``same_scan_direction``, then all ``-->``
    reverse_scan_direction : bool
        If ``reverse_scan_direction``, then ``[<--,-->,<--,-->]`` or all ``<--``

    """
    
    ny, nx = imdark.shape
    chsize = int(nx / nchans)

    imtemp = imdark.copy() if tint is None else imdark * tint

    boxhalf = int(boxsize/2)
    boxsize = int(2*boxhalf + 1)
    distmin = np.ceil(np.sqrt(2.0) * boxhalf)

    # Get rid of pixels around border
    pixmask = ((imtemp>hotcut[0]) & (imtemp<hotcut[1]))
    pixmask[0:4+boxhalf, :] = False
    pixmask[-4-boxhalf:, :] = False
    pixmask[:, 0:4+boxhalf] = False
    pixmask[:, -4-boxhalf:] = False

    # Ignore borders between amplifiers
    for ch in range(1, nchans):
        x1 = ch*chsize - boxhalf
        x2 = x1 + 2*boxhalf
        pixmask[:, x1:x2] = False
    indy, indx = np.where(pixmask)
    nhot = len(indy)
    if nhot < 2:
        if not suppress_error_msg:
            _log.warn("No hot pixels found!")
        return None

    # Only want isolated pixels
    # Get distances for every pixel
    # If too close, then set equal to 0
    for i in range(nhot):
        d = np.sqrt((indx-indx[i])**2 + (indy-indy[i])**2)
        ind_close = np.where((d>0) & (d<distmin))[0]
        if len(ind_close)>0: pixmask[indy[i], indx[i]] = 0
    indy, indx = np.where(pixmask)
    nhot = len(indy)
    if nhot < 2:
        if not suppress_error_msg:
            _log.warn("No hot pixels found!")
        return None

    # Stack all hot pixels in a cube
    hot_all = []
    for iy, ix in zip(indy, indx):
        x1, y1 = np.array([ix,iy]) - boxhalf
        x2, y2 = np.array([x1,y1]) + boxsize
        sub = imtemp[y1:y2, x1:x2]

        # Flip channels along x-axis for PPC
        if calc_ppc:
            # Check if an even or odd channel (index 0)
            for ch in np.arange(0,nchans,2):
                even = True if (ix > ch*chsize) and (ix < (ch+1)*chsize-1) else False
        
            if same_scan_direction:
                flip = True if reverse_scan_direction else False
            elif even:
                flip = True if reverse_scan_direction else False
            else:
                flip = False if reverse_scan_direction else True

            if flip: sub = sub[:,::-1]

        hot_all.append(sub)
    hot_all = np.array(hot_all)

    # Remove average dark current values
    if boxsize>3 and bg_remove==True:
        for im in hot_all:
            im -= np.median([im[0,:], im[:,0], im[-1,:], im[:,-1]])

    # Normalize by sum in 3x3 region
    norm_all = hot_all.copy()
    for im in norm_all:
        im /= im[boxhalf-1:boxhalf+2, boxhalf-1:boxhalf+2].sum()

    # Take average of normalized stack
    ipc_im_avg = np.median(norm_all, axis=0)
    # ipc_im_sig = robust.medabsdev(norm_all, axis=0)

    corner_val = (ipc_im_avg[boxhalf-1,boxhalf-1] + 
                 ipc_im_avg[boxhalf+1,boxhalf+1] + 
                 ipc_im_avg[boxhalf+1,boxhalf-1] + 
                 ipc_im_avg[boxhalf-1,boxhalf+1]) / 4
    if corner_val<0: corner_val = 0

    # Determine post-pixel coupling value?
    if calc_ppc:
        ipc_val = (ipc_im_avg[boxhalf-1,boxhalf] + \
                  ipc_im_avg[boxhalf,boxhalf-1] + \
                  ipc_im_avg[boxhalf+1,boxhalf]) / 3
        if ipc_val<0: ipc_val = 0
            
        ppc_val = ipc_im_avg[boxhalf,boxhalf+1] - ipc_val
        if ppc_val<0: ppc_val = 0

        k_ipc = np.array([[corner_val, ipc_val, corner_val],
                         [ipc_val, 1-4*ipc_val, ipc_val],
                         [corner_val, ipc_val, corner_val]])
        k_ppc = np.zeros([3,3])
        k_ppc[1,1] = 1 - ppc_val
        k_ppc[1,2] = ppc_val
        
        return (k_ipc / k_ipc.sum(), k_ppc / k_ppc.sum())
        
    # Just determine IPC
    else:
        ipc_val = (ipc_im_avg[boxhalf-1,boxhalf] + 
                  ipc_im_avg[boxhalf,boxhalf-1] + 
                  ipc_im_avg[boxhalf,boxhalf+1] + 
                  ipc_im_avg[boxhalf+1,boxhalf]) / 4
        if ipc_val<0: ipc_val = 0

        kernel = np.array([[corner_val, ipc_val, corner_val],
                           [ipc_val, 1-4*ipc_val, ipc_val],
                           [corner_val, ipc_val, corner_val]])
        
        return kernel / kernel.sum()

def plot_kernel(kern, ax=None, return_figax=False):
    """ Plot image of IPC or PPC kernel

    Parameters
    ----------
    kern : ndarray
        Kernel image (3x3 or 5x5, etc) to plot.
    ax : axes
        Axes to plot kernel on. If None, will create new
        figure and axes subplot.
    return_figax : bool
        Return the (figure, axes) for user manipulations?
    """

    if ax is None:
        fig, ax = plt.subplots(1,1, figsize=(5,5))
    else:
        fig = None

    # Convert to log scale for better contrast between pixels
    kern = kern.copy()
    kern[kern==0] = 1e-7
    ny, nx = kern.shape
    extent = np.array([-nx/2,nx/2,-ny/2,ny/2]) 
    ax.imshow(np.log(kern), extent=extent, vmax=np.log(1), vmin=np.log(1e-5))

    # Add text to each pixel position
    for i in range(ny):
        ii = i + int(-ny/2)
        for j in range(nx):
            jj = j + int(-nx/2)
            if (ii==0) and (jj==0): # Different text format at center position
                ax.text(jj,ii, '{:.2f}%'.format(kern[i,j]*100), color='black', fontsize=16,
                    horizontalalignment='center', verticalalignment='center')
            else:
                ax.text(jj,ii, '{:.3f}%'.format(kern[i,j]*100), color='white', fontsize=16,
                    horizontalalignment='center', verticalalignment='center')


    ax.tick_params(axis='both', color='white', which='both')
    for k in ax.spines.keys():
        ax.spines[k].set_color('white')

    if fig is not None:
        ax.set_title('IPC Kernel', fontsize=16)
        fig.tight_layout()

    if return_figax:
        return fig, ax

def plot_dark_histogram(im, ax, binsize=0.0001, return_ax=False, label='Active Pixels', 
                         plot_fit=True, plot_cumsum=True, color='C1', xlim=None, xlim_std=7):
    
    from astropy.modeling import models, fitting
    
    bins = np.arange(im.min(), im.max() + binsize, binsize)
    ig, vg, cv = hist_indices(im, bins=bins, return_more=True)
    # Number of pixels in each bin
    nvals = np.array([len(i) for i in ig])

    # Fit a Gaussian to get peak of dark current
    ind_nvals_max = np.where(nvals==nvals.max())[0][0]
    mn_init = cv[ind_nvals_max]
    std_init = robust.std(im)
    g_init = models.Gaussian1D(amplitude=nvals.max(), mean=mn_init, stddev=std_init)

    fit_g = fitting.LevMarLSQFitter()
    nvals_norm = nvals / nvals.max()
    ind_fit = (cv>mn_init-1*std_init) & (cv<mn_init+1*std_init)
    g_res = fit_g(g_init, cv[ind_fit], nvals_norm[ind_fit])

    bg_max_dn = g_res.mean.value
    bg_max_npix = g_res.amplitude.value

    ax.plot(cv, nvals_norm, label=label, lw=2)
    if plot_fit:
        ax.plot(cv, g_res(cv), label='Gaussian Fit', lw=1.5, color=color)
    label = 'Peak = {:.4f} DN/sec'.format(bg_max_dn)
    ax.plot(2*[bg_max_dn], [0,bg_max_npix], label=label, ls='--', lw=1, color=color)
    if plot_cumsum:
        ax.plot(cv, np.cumsum(nvals) / im.size, color='C3', lw=1, label='Cumulative Sum')
    
    ax.set_ylabel('Relative Number of Pixels')
    ax.set_title('All Active Pixels')

    if xlim is None:
        xlim = np.array([-1,1]) * xlim_std * g_res.stddev.value + bg_max_dn
        xlim[0] = np.min([0,xlim[0]])

    ax.set_xlabel('Dark Rate (DN/sec)')

    ax.set_xlim(xlim)#[0,2*bg_max_dn])
    ax.legend(loc='upper left')

    if return_ax:
        return ax

def calc_ktc(bias_sigma_arr, binsize=0.25, return_std=False):
    """ Calculate kTC (Reset) Noise

    Use the uncertainty image from super bias to calculate
    the kTC noise. This function generates a histogram of
    the pixel uncertainties and takes the peak of the 
    distribution as the pixel reset noise.

    Parameters
    ----------
    bias_sigma_arr : ndarray
        Image of the pixel uncertainties.
    binsize : float
        Size of the histogram bins.
    return_std : bool
        Also return the standard deviation of the 
        distribution?
    
    """

    im = bias_sigma_arr
    binsize = binsize
    bins = np.arange(im.min(), im.max() + binsize, binsize)
    ig, vg, cv = hist_indices(im, bins=bins, return_more=True)

    nvals = np.array([len(i) for i in ig])
    # nvals_rel = nvals / nvals.max()

    # Peak of distribution
    ind_peak = np.where(nvals==nvals.max())[0][0]
    peak = cv[ind_peak]

    if return_std:
        return peak, robust.medabsdev(im)
    else:
        return peak


def pow_spec_ramp(data, nchan, nroh=0, nfoh=0, nframes=1, expand_npix=False,
                  same_scan_direction=False, reverse_scan_direction=False,
                  mn_func=np.mean, return_freq=False, dt=1, **kwargs):
    """ Get power spectrum within frames of input ramp
    
    Takes an input cube, splits it into output channels, and
    finds the power spectrum of each frame. Then, calculate 
    the average power spectrum for each channel.
    
    Use `nroh` and `nfoh` to expand the frame size to encapsulate
    the row and frame overheads not included in the science data.
    These just zero-pad the array.
    
    Parameters
    ==========
    data : ndarray
        Input Image cube.
    nchan : int
        Number of amplifier channels.
    nroh : int
        Number of pixel overheads per row.
    nfoh : int
        Number of row overheads per frame.
    nframes : int
        Number of frames to use to calculate an power spectrum.
        Normally we just use 1 frame time
    expand_npix : bool
        Should we zero-pad the array to a power of two factor
        for incresed speed?
    same_scan_direction : bool
        Are all the output channels read in the same direction?
        By default fast-scan readout direction is ``[-->,<--,-->,<--]``
        If ``same_scan_direction``, then all ``-->``
    reverse_scan_direction : bool
        If ``reverse_scan_direction``, then ``[<--,-->,<--,-->]`` or all ``<--``
    """
    
    nz, ny, nx = data.shape
    chsize = int(nx / nchan)

    # Channel size and ny plus pixel and row overheads
    ch_poh = chsize + nroh
    ny_poh = ny + nfoh
    
    ps_data = [] # Hold channel data
    for ch in range(nchan):
        # Array of pixel values
        if (nroh>0) or (nfoh>0):
            sig = np.zeros([nz,ny_poh,ch_poh])
            sig[:,0:ny,0:chsize] += data[:,:,ch*chsize:(ch+1)*chsize]
        else:
            sig = data[:,:,ch*chsize:(ch+1)*chsize]
            
        # Flip x-axis for odd channels
        if same_scan_direction:
            flip = True if reverse_scan_direction else False
        elif np.mod(ch,2)==0:
            flip = True if reverse_scan_direction else False
        else:
            flip = False if reverse_scan_direction else True
        sig = sig[:,:,::-1] if flip else sig
        
        if nframes==1:
        
            sig = sig.reshape([sig.shape[0],-1])
            npix = sig.shape[1]

            # Pad nsteps to a power of 2, which can be faster
            npix2 = int(2**np.ceil(np.log2(npix))) if expand_npix else npix

            # Power spectrum of each frame
            ps = np.abs(np.fft.rfft(sig, n=npix2))**2 / npix2
            
        else:
            
            sh = sig.shape
            npix = nframes * sh[-2] * sh[-1]
            # Pad nsteps to a power of 2, which can be faster
            npix2 = int(2**np.ceil(np.log2(npix))) if expand_npix else npix
            
            # Power spectrum for each set of frames
            niter = nz - nframes + 1
            ps = []
            for i in range(niter):
                sig2 = sig[i:i+nframes].ravel()

                # Power spectrum
                ps.append(np.abs(np.fft.rfft(sig2, n=npix2))**2 / npix2)
            ps = np.array(ps)
                
        # Average of all power spectra
        ps_data.append(mn_func(ps, axis=0))

    # Power spectrum of each output channel
    ps_data = np.array(ps_data)
    
    if return_freq:
        freq = get_freq_array(ps_data, dt=dt)
        return ps_data, freq
    else:
        return ps_data 


def pow_spec_ramp_pix(data, nchan, expand_nstep=False,
                      mn_func=np.mean, return_freq=False, dt=1, **kwargs):
    """ Get power spectrum of pixels within ramp
    
    Takes an input cube, splits it into output channels, and
    finds the power spectrum of each pixel. Return the average 
    power spectrum for each channel.
    
    Parameters
    ==========
    data : ndarray
        Input Image cube.
    nchan : int
        Number of amplifier channels.
    expand_nstep : bool
        Should we zero-pad the array to a power of two factor
        for incresed speed?
    """
    
    nz, ny, nx = data.shape
    chsize = int(nx / nchan)
    
    ps_data = [] # Hold channel data
    for ch in range(nchan):
        # Array of pixel values
        sig = data[:,:,ch*chsize:(ch+1)*chsize]

        sig = sig.reshape([sig.shape[0],-1])
        nstep = sig.shape[0]

        # Pad nsteps to a power of 2, which can be faster
        nstep2 = int(2**np.ceil(np.log2(nstep))) if expand_nstep else nstep

        # Power spectrum of each pixel
        ps = np.abs(np.fft.rfft(sig, n=nstep2, axis=0))**2 / nstep2

        # Average of all power spectra
        ps_data.append(mn_func(ps, axis=1))

    # Power spectrum of each output channel
    ps_data = np.array(ps_data)
    
    if return_freq:
        freq = get_freq_array(ps_data, dt=dt)
        return ps_data, freq
    else:
        return ps_data 


def fit_corr_powspec(freq, ps, flim1=[0,1], flim2=[10,100], alpha=-1, **kwargs):
    """ Fit Correlated Noise Power Spectrum

    Fit the scaling factors of the 1/f power law components
    observed in the correlated noise power spectra. This
    function separately calculates the high-freq and low-
    freq scale factor components defined by the fcut params.
    The mid-frequency ranges are interpolated in log space.

    Parameters
    ==========
    freq : ndarray
        Input frequencies corresponding to power spectrum.
    ps : ndarray
        Input power spectrum to fit.
    flim1 : float
        Fit frequencies within this range to get scaling
        for low frequency 1/f noise.
    flim2 : float
        Fit frequencies within this range to get scaling
        for high frequency 1/f noise.
    alpha : float
        Noise power spectrum scaling
    """
    
    yf = freq**alpha
    yf[0] = 0
    
    # Low frequency fit
    ind = (freq >= flim1[0]) & (freq <= flim1[1]) & (yf > 0)
    scl1 = np.median(ps[ind] / yf[ind])

    # High frequency fit
    ind = (freq >= flim2[0]) & (freq <= flim2[1]) & (yf > 0)
    scl2 = np.median(ps[ind] / yf[ind])

    return np.array([scl1, scl2])

def broken_pink_powspec(freq, scales, fcut1=1, fcut2=10, alpha=-1, **kwargs):

    scl1, scl2 = scales
    yf = freq**alpha
    yf[0] = 0

    # Output array
    res = np.zeros(len(yf))

    # Low frequency component
    ind = (freq <= fcut1)
    res[ind] = scl1*yf[ind]

    # High frequency componet
    ind = (freq >= fcut2)
    res[ind] = scl2*yf[ind]

    # Mid frequency interpolation, log space
    ind = (freq > fcut1) & (freq < fcut2)
    xlog = np.log10(freq)
    ylog = np.log10(res)
    ylog[ind] = np.interp(xlog[ind], xlog[~ind], ylog[~ind])
    res[ind] = 10**ylog[ind]

    return res

def get_power_spec(data, nchan=4, calc_cds=True, kw_powspec=None, per_pixel=False,
                   return_corr=False, return_ucorr=False, mn_func=np.mean):
    """
    Calculate the power spectrum of an input data ramp in a variety of ways.

    If return_corr and return_ucorr are both False, then will return (ps_all, None, None).

    Parameters
    ==========
    calc_cds : bool
        Power spectrum of CDS pairs or individual frames?
    per_pixel : bool
        Calculate average power spectrum of each pixel along ramp (frame timescales)?
        If False, samples pixels within a frame (pixel read timescales)
    return_corr : bool
        Return power spectrum of channel correlated 1/f noise?
    return_ucorr : bool
        Return power spectra of channel-dependent (uncorrelated) 1/f noise?
    kw_powspec : dict
        Keyword arguments to pass to `pow_spec_ramp` function.
    mn_func : func
        Function to use to perform averaging of individual power spectra.
    """

    nz, ny, nx = data.shape
    chsize = int(nx/nchan)

    # CDS or just subtract first frame
    if calc_cds:
        cds = data[1::2] - data[0::2]
    else:
        cds = data[1:] - data[0]

    # Remove averages from each frame
    cds_mn = np.median(cds.reshape([cds.shape[0], -1]), axis=1)
    cds -= cds_mn.reshape([-1,1,1])

    # Remove averages from each pixel
    cds_mn = np.median(cds, axis=0)
    cds -= cds_mn

    # Keywords for power spectrum
    # Only used for pow_spec_ramp, not pow_spec_ramp_pix
    if kw_powspec is None:
        kw_powspec = {
            'nroh': 0, 'nfoh': 0, 'nframes': 1,
            'same_scan_direction': False, 'reverse_scan_direction': False
        }
        same_scan_direction = kw_powspec['same_scan_direction']

    # Power spectrum of all frames data
    if per_pixel:
        ps_full = pow_spec_ramp_pix(cds, nchan, mn_func=mn_func)
    else:
        ps_full = pow_spec_ramp(cds, nchan, mn_func=mn_func, **kw_powspec)

    # Extract 1/f noise from data
    ps_corr, ps_ucorr = (None, None)
    if return_ucorr or return_corr:
        fn_data = []
        for im in cds:
            ch_arr = im.reshape([ny,-1,chsize]).transpose([1,0,2])
            mask = np.abs(im - np.median(im)) > 10*robust.medabsdev(im)
            mask = mask.reshape([ny,-1,chsize]).transpose([1,0,2])
            fnoise = channel_smooth_savgol(ch_arr, mask=mask)
            fnoise = fnoise.transpose([1,0,2]).reshape([ny,nx])
            fn_data.append(fnoise)
        fn_data = np.array(fn_data)

        # Delete data and cds arrays to free up memory
        del cds

        # Split into correlated and uncorrelated components
        fn_data_corr = []
        for j, im in enumerate(fn_data):
            # Extract correlated 1/f noise data        
            fn_corr = channel_averaging(im, nchans=nchan,  off_chans=False,
                same_scan_direction=same_scan_direction, mn_func=np.mean)
            # Subtract correlated noise from fn_data
            if return_ucorr: 
                fn_data[j] -= fn_corr
            # Only append first channel since the rest are the same data
            fn_data_corr.append(fn_corr[:,0:chsize])
        fn_data_corr = np.array(fn_data_corr)
        
        # Power spectrum of uncorrelated 1/f noise
        if return_ucorr:
            if per_pixel:
                ps_ucorr = pow_spec_ramp_pix(fn_data, nchan, mn_func=mn_func) 
            else:
                ps_ucorr = pow_spec_ramp(fn_data, nchan, mn_func=mn_func, **kw_powspec)
        del fn_data

        # Power spectrum of correlated 1/f noise
        if return_corr:
            if per_pixel:
                ps_corr = pow_spec_ramp_pix(fn_data_corr, 1, mn_func=mn_func)
            else:
                ps_corr = pow_spec_ramp(fn_data_corr, 1, mn_func=mn_func, **kw_powspec)
        del fn_data_corr

    return ps_full, ps_ucorr, ps_corr



def get_power_spec_all(allfiles, super_bias=None, det=None, DMS=False, include_oh=False, 
                       same_scan_direction=False, reverse_scan_direction=False,
                       calc_cds=True, return_corr=False, return_ucorr=False, 
                       per_pixel=False, mn_func=np.mean, kw_reffix=None):
    
    """
    Return the average power spectra (white, 1/f noise correlated and uncorrelated) of
    all FITS files. 

    Parameters
    ==========
    allfiles : array-like
        List of FITS files to operate on.
    super_bias : ndarray
        Option to subtract a super bias image from all frames in a ramp.
        Provides slightly better statistical averaging for reference pixel
        correction routines.
    det : Detector class
        Option to pass known NIRCam detector class. This will get generated 
        from a FITS header if not specified.
    DMS : bool
        Are the files DMS formatted or FITSWriter?
    include_oh : bool
        Zero-pad the data to insert line and frame overhead pixels? 
    same_scan_direction : bool
        Are all the output channels read in the same direction?
        By default fast-scan readout direction is ``[-->,<--,-->,<--]``
        If ``same_scan_direction``, then all ``-->``
    reverse_scan_direction : bool
        If ``reverse_scan_direction``, then ``[<--,-->,<--,-->]`` or all ``<--``
    calc_cds : bool
        Power spectrum of CDS pairs or individual frames?
    per_pixel : bool
        Calculate average power spectrum of each pixel along ramp (frame timescales)?
        If False, samples pixels within a frame (pixel read timescales)
    return_corr : bool
        Return power spectrum of channel correlated 1/f noise?
    return_ucorr : bool
        Return power spectra of channel-dependent (uncorrelated) 1/f noise?
    kw_powspec : dict
        Keyword arguments to pass to `pow_spec_ramp` function.
    mn_func : func
        Function to use to perform averaging of individual power spectra.
    """


    # Set logging to WARNING to suppress messages
    log_prev = pynrc.conf.logging_level
    pynrc.setup_logging('WARNING', verbose=False)

    if super_bias is None:
        super_bias = 0
        
    # nfiles = len(allfiles)
    
    # Header info from first file
    if det is None:
        hdr = fits.getheader(allfiles[0])
        det = create_detops(hdr, DMS=DMS)

    # Overhead information
    nchan = det.nout

    # Row and frame overheads
    if include_oh:
        nroh = det._line_overhead
        nfoh = det._extra_lines
    else:
        nroh = nfoh = 0

    # Keywords for reffix
    if kw_reffix is None:
        kw_reffix = {
            'nchans': nchan, 'altcol': True, 'in_place': True,
            'fixcol': False, 'avg_type': 'pixel', 'savgol': True, 'perint': False
        }

    # Keywords for power spectrum
    kw_powspec = {
        'nroh': nroh, 'nfoh': nfoh, 'nframes': 1,
        'same_scan_direction': same_scan_direction,
        'reverse_scan_direction': reverse_scan_direction
    }

    pow_spec_all = []
    if return_corr: pow_spec_corr = []
    if return_ucorr: pow_spec_ucorr = []

    for fname in tqdm(allfiles, desc='Files'):

        # If DMS, then might be multiple integrations per FITS file
        nint = fits.getheader(fname)['NINTS'] if DMS else 1

        for i in trange(nint, leave=False, desc='Ramps'):
            data = get_fits_data(fname, bias=super_bias, reffix=True, 
                                DMS=DMS, int_ind=i, **kw_reffix)

            ps_full, ps_ucorr, ps_corr = get_power_spec(data, nchan=nchan, 
                calc_cds=calc_cds, return_corr=return_corr, return_ucorr=return_ucorr, 
                per_pixel=per_pixel, mn_func=mn_func, kw_powspec=kw_powspec)

            pow_spec_all.append(ps_full)
            if return_corr: 
                pow_spec_corr.append(ps_corr)
            if return_ucorr: 
                pow_spec_ucorr.append(ps_ucorr)
            
            del data

    # Full spectra
    pow_spec_all = np.array(pow_spec_all)
    ps_all = np.mean(pow_spec_all, axis=0)
    # Correlated Noise
    if return_corr: 
        pow_spec_corr = np.array(pow_spec_corr)
        ps_corr = np.mean(pow_spec_corr, axis=0).squeeze()
    else:
        ps_corr = None

    # Uncorrelated Noise per amplifier channel
    if return_ucorr:
        pow_spec_ucorr = np.array(pow_spec_ucorr)
        ps_ucorr = np.mean(pow_spec_all, axis=0)
    else:
        ps_ucorr = None

    # Set back to previous logging level
    pynrc.setup_logging(log_prev, verbose=False)

    return ps_all, ps_corr, ps_ucorr

def get_freq_array(pow_spec, dt=1, nozero=False, npix_odd=False):
    """ Return frequencies associated with power spectrum
    
    Parameters
    ==========
    pow_spec : ndarray
        Power spectrum to obtain associated frequency array.
    dt : float
        Delta time between corresponding elements in time domain.
    nozero : bool
        Set freq[0] = freq[1] to remove zeros? This is mainly so
        we don't obtain NaN's later when calculating 1/f noise.
    npix_odd : bool
        We normally assume that the original time-domain data
        was comprised an even number of pixels. However, if it
        were actually odd, the frequency array will be slightly
        shifted. Set this to True if the intrinsic data that was
        used to generate the pow_spec had an odd number of elements.
    """

    # This assumes an even input array
    npix = 2 * (pow_spec.shape[-1] - 1)
    # Off by 1 if initial npix was odd
    if npix_odd:
        npix += 1
    freq = np.fft.rfftfreq(npix, d=dt)

    if nozero:
        # First element should not be 0
        freq[0] = freq[1]

    return freq
