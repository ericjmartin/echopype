import pynmea2
from datetime import datetime as dt
import xarray as xr
import numpy as np
import zarr
import netCDF4
from .._version import get_versions
from ..utils import io
ECHOPYPE_VERSION = get_versions()['version']
del get_versions

COMPRESSION_SETTINGS = {
    'netcdf4': {'zlib': True, 'complevel': 4},
    'zarr': {'compressor': zarr.Blosc(cname='zstd', clevel=3, shuffle=2)}
}


class SetGroupsBase:
    """Base class for saving groups to netcdf or zarr from echosounder data files.
    """
    def __init__(self, parser_obj, input_file, output_path, sonar_model=None,
                 engine='zarr', compress=True, overwrite=True, params=None):
        # TODO: Change convert_obj to parse_obj
        self.parser_obj = parser_obj   # parser object ParseEK60/ParseAZFP/etc...
        self.sonar_model = sonar_model   # Used for when a sonar that is not AZFP/EK60/EK80 can still be saved
        self.input_file = input_file
        self.output_path = output_path
        self.engine = engine
        self.compress = compress
        self.overwrite = overwrite
        self.ui_param = params

        if not self.compress:
            self.compression_settings = None
        else:
            self.compression_settings = COMPRESSION_SETTINGS[self.engine]

    def save(self):
        """Actually save groups to file by calling the set methods.
        """

    # TODO: change the set_XXX methods to return a dataset to be saved in the overarching save method
    def set_toplevel(self, sonar_model, date_created=None):
        """Set the top-level group.
        """
        # Collect variables
        if date_created is None:
            # TODO: change below to use time of config datagram
            # Check if AZFP or EK
            if isinstance(self.parser_obj.ping_time, list):
                date_created = self.parser_obj.ping_time[0]
            else:
                pt = []
                for v in self.parser_obj.ping_time.values():
                    pt.append(v[0])
                date_created = np.sort(pt)[0]

        tl_dict = {'conventions': 'CF-1.7, SONAR-netCDF4-1.0, ACDD-1.3',
                   'keywords': sonar_model,
                   'sonar_convention_authority': 'ICES',
                   'sonar_convention_name': 'SONAR-netCDF4',
                   'sonar_convention_version': '1.0',
                   'summary': '',
                   'title': '',
                   'date_created': np.datetime_as_string(date_created, 's') + 'Z',
                   'survey_name': self.ui_param['survey_name']}
        # Add any extra user defined values
        for k, v in list(self.ui_param.items())[5:]:
            tl_dict[k] = v

        # Save
        if self.engine == 'netcdf4':
            with netCDF4.Dataset(self.output_path, "w", format="NETCDF4") as ncfile:
                [ncfile.setncattr(k, v) for k, v in tl_dict.items()]
        elif self.engine == 'zarr':
            zarrfile = zarr.open(self.output_path, mode="w")
            for k, v in tl_dict.items():
                zarrfile.attrs[k] = v
        else:
            raise ValueError("Unsupported file format")

    def set_provenance(self):
        """Set the Provenance group.
        """
        # Collect variables
        prov_dict = {'conversion_software_name': 'echopype',
                     'conversion_software_version': ECHOPYPE_VERSION,
                     'conversion_time': dt.utcnow().isoformat(timespec='seconds') + 'Z',    # use UTC time
                     'src_filenames': self.input_file}
        # Save
        if self.engine == 'netcdf4':
            with netCDF4.Dataset(self.output_path, "a", format="NETCDF4") as ncfile:
                prov = ncfile.createGroup("Provenance")
                [prov.setncattr(k, v) for k, v in prov_dict.items()]
        elif self.engine == 'zarr':
            zarr_file = zarr.open(self.output_path, mode="a")
            prov = zarr_file.create_group('Provenance')
            for k, v in prov_dict.items():
                prov.attrs[k] = v
        else:
            raise ValueError("Unsupported file format")

    def set_sonar(self, sonar_vals):
        """Set the Sonar group.
        """
        # Collect variables
        sonar_dict = dict(zip(('sonar_manufacturer', 'sonar_model', 'sonar_serial_number',
                               'sonar_software_name', 'sonar_software_version', 'sonar_type'), sonar_vals))

        # Save variables
        if self.engine == 'netcdf4':
            with netCDF4.Dataset(self.output_path, "a", format="NETCDF4") as ncfile:
                snr = ncfile.createGroup("Sonar")
                # set group attributes
                [snr.setncattr(k, v) for k, v in sonar_dict.items()]

        elif self.engine == 'zarr':
            zarrfile = zarr.open(self.output_path, mode='a')
            snr = zarrfile.create_group('Sonar')

            for k, v in sonar_dict.items():
                snr.attrs[k] = v

    def set_nmea(self):
        """Set the Platform/NMEA group.
        """
        # Save nan if nmea data is not encoded in the raw file
        if len(self.parser_obj.nmea['nmea_string']) != 0:
            # Convert np.datetime64 numbers to seconds since 1900-01-01
            # due to xarray.to_netcdf() error on encoding np.datetime64 objects directly
            time = (self.parser_obj.nmea['timestamp'] -
                    np.datetime64('1900-01-01T00:00:00')) / np.timedelta64(1, 's')
            raw_nmea = self.parser_obj.nmea['nmea_string']
        else:
            time = [np.nan]
            raw_nmea = [np.nan]

        ds = xr.Dataset(
            {'NMEA_datagram': (['location_time'], raw_nmea,
                               {'long_name': 'NMEA datagram'})
             },
            coords={'location_time': (['location_time'], time,
                                      {'axis': 'T',
                                       'calendar': 'gregorian',
                                       'long_name': 'Timestamps for NMEA datagrams',
                                       'standard_name': 'time',
                                       'units': 'seconds since 1900-01-01'})},
            attrs={'description': 'All NMEA sensor datagrams'})

        # save to file
        io.save_file(ds, path=self.output_path, mode='a', engine=self.engine,
                     group='Platform/NMEA', compression_settings=self.compression_settings)

    # TODO: move this to be part of parser as it is not a "set" operation
    def _parse_NMEA(self):
        """Get the lat and lon values from the raw nmea data"""
        messages = [string[3:6] for string in self.parser_obj.nmea['nmea_string']]
        idx_loc = np.argwhere(np.isin(messages,
                                      self.ui_param['nmea_gps_sentence'])).squeeze()
        if idx_loc.size == 1:  # in case of only 1 matching message
            idx_loc = np.expand_dims(idx_loc, axis=0)
        nmea_msg = [pynmea2.parse(self.parser_obj.nmea['nmea_string'][x]) for x in idx_loc]
        lat = np.array([x.latitude if hasattr(x, 'latitude') else np.nan
                        for x in nmea_msg]) if nmea_msg else [np.nan]
        lon = np.array([x.longitude if hasattr(x, 'longitude') else np.nan
                        for x in nmea_msg]) if nmea_msg else [np.nan]
        msg_type = np.array([x.sentence_type if hasattr(x, 'sentence_type') else np.nan
                             for x in nmea_msg]) if nmea_msg else [np.nan]
        location_time = (np.array(self.parser_obj.nmea['timestamp'])[idx_loc] -
                         np.datetime64('1900-01-01T00:00:00')) / np.timedelta64(1, 's') if nmea_msg else [np.nan]

        return location_time, msg_type, lat, lon