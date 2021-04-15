# -*- coding: utf-8 -*-
"""General and basic API for models in HydroMT"""

from abc import ABCMeta, abstractmethod, abstractproperty
import os
from os.path import join, isdir, dirname, basename, isfile, abspath, exists
import xarray as xr
import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.geometry import box
import logging
from pathlib import Path
from functools import wraps
import inspect

from ..data_adapter import DataCatalog
from .. import config

__all__ = ["Model"]

logger = logging.getLogger(__name__)


class Model(object, metaclass=ABCMeta):
    """General and basic API for models in HydroMT"""

    # FIXME
    _DATADIR = ""  # path to the model data folder
    _NAME = "modelname"
    _CONF = "model.ini"
    _CF = dict()  # configreader kwargs
    _GEOMS = {"<general_hydromt_name>": "<model_name>"}
    _MAPS = {"<general_hydromt_name>": "<model_name>"}
    _FOLDERS = [""]

    def __init__(
        self, root=None, mode="w", config_fn=None, data_libs=None, logger=logger
    ):
        self.logger = logger

        # link to data
        self.data_catalog = DataCatalog(logger=self.logger)  # data sources
        # self.data_catalog.from_global_sources()
        if data_libs is not None:
            self.data_catalog.from_yml(data_libs)

        # model paths
        self._config_fn = self._CONF if config_fn is None else config_fn
        self.set_root(root, mode)

        # placeholders
        self._staticmaps = xr.Dataset()
        self._staticgeoms = dict()  # dictionnary of gdp.GeoDataFrame
        self._forcing = dict()  # dictionnary of xr.DataArray
        self._config = dict()  # nested dictionary
        self._states = dict()  # dictionnary of xr.DataArray
        self._results = dict()  # dictionnary of xr.DataArray

    def _check_get_opt(self, opt):
        """Check all opt keys and raise sensible error messages if unknonwn."""
        for method in opt.keys():
            m = method.strip("0123456789")
            if not callable(getattr(self, m, None)):
                if not hasattr(self, m) and hasattr(self, f"setup_{m}"):
                    raise DeprecationWarning(
                        f'Use full name "setup_{method}" instead of "{method}"'
                    )
                else:
                    raise ValueError(f'Model {self._NAME} has no method "{method}"')
        return opt

    def _run_log_method(self, method, *args, **kwargs):
        """Log method paramters before running a method"""
        method = method.strip("0123456789")
        func = getattr(self, method)
        signature = inspect.signature(func)
        for i, (k, v) in enumerate(signature.parameters.items()):
            v = kwargs.get(k, v.default)
            if v is inspect.Parameter.empty:
                if len(args) >= i + 1:
                    v = args[i]
                else:
                    continue
            self.logger.debug(f"{method}.{k}: {v}")
        return func(*args, **kwargs)

    def build(self, region, res=None, write=True, opt=None):
        """Single method to setup and write a full model schematization and
        configuration from scratch"""
        opt = self._check_get_opt(opt)

        # run setup_config and setup_basemaps first!
        self._run_log_method("setup_config", **opt.pop("setup_config", {}))
        kwargs = opt.pop("setup_basemaps", {})
        kwargs.update(region=region)
        if res is not None:
            kwargs.update(res=res)
        self._run_log_method("setup_basemaps", **kwargs)

        # then loop over other methods
        for method in opt:
            self._run_log_method(method, **opt[method])

        # write
        if write:
            self.write()

    def update(self, model_out=None, write=True, opt=None):
        """Single method to setup and write a full model schematization and
        configuration from scratch"""
        opt = self._check_get_opt(opt)

        # read current model
        if not self._write:
            if model_out is None:
                raise ValueError(
                    '"model_out" directory required when updating in "read-only" mode'
                )
            self.read()
            self.set_root(model_out, mode="w")

        # check if model has a region
        if self.region is None:
            raise ValueError(
                'Model region not found, setup basemaps using "build" method first.'
            )

        # remove setup_basemaps from options and throw warning
        if "setup_basemaps" in opt:
            opt.pop("setup_basemaps")  # remove from opt
            self.logger.warning(
                '"setup_basemaps" can only be called when building a model.'
            )

        # loop over other methods from ini file
        self._run_log_method("setup_config", **opt.pop("setup_config", {}))
        for method in opt:
            self._run_log_method(method, **opt[method])

        # write
        if write:
            self.write()

    ## file system

    @property
    def root(self):
        """Path to model folder."""
        if self._root is None:
            raise ValueError("Root unknown, use set_root method")
        return self._root

    def set_root(self, root, mode="w"):
        """Initialized the model root.
        In read mode it checks if the root exists.
        In write mode in creates the required model folder structure

        Parameters
        ----------
        root : str, optional
            path to model root
        mode : {"r", "r+", "w"}, optional
            read/write-only mode for model files
        """
        if mode not in ["r", "r+", "w"]:
            raise ValueError(f'mode "{mode}" unknown, select from "r", "r+" or "w"')
        old_root = getattr(self, "_root", None)
        self._root = root if root is None else abspath(root)
        self._read = mode.startswith("r")
        self._write = mode != "r"
        if root is not None:
            if self._write:
                for name in self._FOLDERS:
                    path = join(self._root, name)
                    if not isdir(path):
                        os.makedirs(path)
                    elif not self._read:
                        self.logger.warning(
                            "Model dir already exists and "
                            f"files might be overwritten: {path}."
                        )
            # check directory
            elif not isdir(self._root):
                raise IOError(f'model root not found at "{self._root}"')

    ## I/O

    @abstractmethod
    def read(self):
        """Method to read the complete model schematization and configuration from file."""
        self.read_config()
        self.read_staticmaps()

    @abstractmethod
    def write(self):
        """Method to write the complete model schematization and configuration to file."""
        self.write_config()
        self.write_staticmaps()

    def _configread(self, fn):
        return config.configread(fn, abs_path=False)

    def _configwrite(self, fn):
        return config.configwrite(fn, self.config)

    def read_config(self, config_fn=None):
        """Initialize default config and fill with config at <config_fn>"""
        if config_fn is not None:
            self._config_fn = config_fn
        cfdict = dict()
        if not self._read:
            fn_def = join(self._DATADIR, self._NAME, self._CONF)
            if isfile(fn_def):
                cfdict = self._configread(fn_def)
                self.logger.debug(f"Default config read from {fn_def}")
        elif self.root is not None and self._config_fn is not None:
            fn = join(self.root, self._config_fn)
            if isfile(fn):
                cfdict = self._configread(fn)
                self.logger.debug(f"Model config read from {fn}")
        self._config = cfdict

    def write_config(self, config_name=None, config_root=None):
        """Write config to <root/config_fn>"""
        if config_name is not None:
            self._config_fn = config_name
        elif self._config_fn is None:
            self._config_fn = self._CONF
        if config_root is None:
            config_root = self.root
        fn = join(config_root, self._config_fn)
        self.logger.info(f"Writing model config to {fn}")
        self._configwrite(fn)

    @abstractmethod
    def read_staticmaps(self):
        """Read staticmaps at <root/?/> and parse to xarray Dataset"""
        # to read gdal raster files use: hydromt.open_mfraster()
        # to read netcdf use: xarray.open_dataset()
        if not self._write:
            # start fresh in read-only mode
            self._staticmaps = xr.Dataset()
        raise NotImplementedError()

    @abstractmethod
    def write_staticmaps(self):
        """Write staticmaps at <root/?/> in model ready format """
        # to write to gdal raster files use: self.staticmaps.rio.to_mapstack()
        # to write to netcdf use: self.staticmaps.to_netcdf()
        if not self._write:
            raise IOError("Model opened in read-only mode")
        raise NotImplementedError()

    @abstractmethod
    def read_staticgeoms(self):
        """Read staticgeoms at <root/?/> and parse to dict of geopandas"""
        if not self._write:
            # start fresh in read-only mode
            self._staticgeoms = dict()
        raise NotImplementedError()

    @abstractmethod
    def write_staticgeoms(self):
        """Write staticmaps at <root/?/> in model ready format """
        # to write use self.staticgeoms[var].to_file()
        if not self._write:
            raise IOError("Model opened in read-only mode")
        raise NotImplementedError()

    @abstractmethod
    def read_forcing(self):
        """Read forcing at <root/?/> and parse to dict of xr.DataArray"""
        if not self._write:
            # start fresh in read-only mode
            self._forcing = dict()
        raise NotImplementedError()

    @abstractmethod
    def write_forcing(self):
        """write forcing at <root/?/> in model ready format"""
        if not self._write:
            raise IOError("Model opened in read-only mode")
        raise NotImplementedError()

    @abstractmethod
    def read_states(self):
        """Read states at <root/?/> and parse to dict of xr.DataArray"""
        if not self._write:
            # start fresh in read-only mode
            self._states = dict()
        raise NotImplementedError()

    @abstractmethod
    def write_states(self):
        """write states at <root/?/> in model ready format"""
        if not self._write:
            raise IOError("Model opened in read-only mode")
        raise NotImplementedError()

    @abstractmethod
    def read_results(self):
        """Read results at <root/?/> and parse to dict of xr.DataArray"""
        if not self._write:
            # start fresh in read-only mode
            self._results = dict()
        raise NotImplementedError()

    @abstractmethod
    def write_results(self):
        """write results at <root/?/> in model ready format"""
        if not self._write:
            raise IOError("Model opened in read-only mode")
        raise NotImplementedError()

    ## model configuration

    @property
    def config(self):
        """Returns parsed model configuration."""
        if not self._config:
            self.read_config()  # initialize default config
        return self._config

    def set_config(self, *args):
        """Update the config dictionary at key(s) with values.

        Parameters
        ----------
        args : key(s), value tuple, with minimal length of two
            keys can given by multiple args: ('key1', 'key2', 'value')
            or a string with '.' indicating a new level: ('key1.key2', 'value')

        Examples
        --------
        >> # self.config = {'a': 1, 'b': {'c': {'d': 2}}}

        >> set_config('a', 99)
        >> {'a': 99, 'b': {'c': {'d': 2}}}

        >> set_config('b', 'c', 'd', 99) # identical to set_config('b.d.e', 99)
        >> {'a': 1, 'b': {'c': {'d': 99}}}
        """
        if not self._write:
            raise IOError("Model opened in read-only mode")
        if len(args) < 2:
            TypeError("set_config() requires a least one key and one value.")
        if not self.config:
            self._config = dict()
        args = list(args)
        value = args.pop(-1)
        if len(args) == 1 and "." in args[0]:
            args = args[0].split(".") + args[1:]
        branch = self._config
        for key in args[:-1]:
            if not key in branch or not isinstance(branch[key], dict):
                branch[key] = {}
            branch = branch[key]
        branch[args[-1]] = value

    def setup_config(self, **cfdict):
        """Update config with a dictionary"""
        # TODO rename to update_config
        if len(cfdict) > 0:
            self.logger.debug(f"Setting model config options.")
        for key, value in cfdict.items():
            self.set_config(key, value)

    def get_config(self, *args, fallback=None, abs_path=False):
        """Get a config value at key(s).

        Parameters
        ----------
        args : tuple or string
            keys can given by multiple args: ('key1', 'key2')
            or a string with '.' indicating a new level: ('key1.key2')
        fallback: any, optional
            fallback value if key(s) not found in config, by default None.
        abs_path: bool, optional
            If True return the absolute path relative to the model root, by deafult False.
            NOTE: this assumes the config is located in model root!

        Returns
        value : any type
            dictionary value

        Examples
        --------
        >> # self.config = {'a': 1, 'b': {'c': {'d': 2}}}

        >> get_config('a')
        >> 1

        >> get_config('b', 'c', 'd') # identical to get_config('b.c.d')
        >> 2

        >> get_config('b.c') # # identical to get_config('b','c')
        >> {'d': 2}
        """
        args = list(args)
        if len(args) == 1 and "." in args[0]:
            args = args[0].split(".") + args[1:]
        branch = self._config
        for key in args[:-1]:
            branch = branch.get(key, {})
            if not isinstance(branch, dict):
                branch = dict()
                break
        value = branch.get(args[-1], fallback)
        if abs_path and isinstance(value, str):
            value = Path(value)
            if not value.is_absolute():
                value = Path(abspath(join(self.root, value)))
        return value

    ## model parameter maps, geometries and spatial properties

    @property
    def staticmaps(self):
        """xarray.dataset representation of all parameter maps"""
        if len(self._staticmaps) == 0:
            if self._read:
                self.read_staticmaps()
            else:
                self.logger.warning("No staticmaps defined")
        return self._staticmaps

    def set_staticmaps(self, data, name=None):
        """Add data to staticmaps"""
        if name is None:
            if isinstance(data, xr.DataArray) and data.name is not None:
                name = data.name
            elif not isinstance(data, xr.Dataset):
                raise ValueError("Setting a map requires a name")
        elif name is not None and isinstance(data, xr.Dataset):
            data_vars = list(data.data_vars)
            if len(data_vars) == 1 and name not in data_vars:
                data = data.rename_vars({data_vars[0]: name})
            elif name not in data_vars:
                raise ValueError("Name not found in DataSet")
            else:
                data = data[[name]]
        if isinstance(data, xr.DataArray):
            data.name = name
            data = data.to_dataset()
        if len(self._staticmaps) == 0:  # new data
            if not isinstance(data, xr.Dataset):
                raise ValueError("First parameter map(s) should xarray.Dataset")
            self._staticmaps = data
        else:
            if isinstance(data, np.ndarray):
                if data.shape != self.shape:
                    raise ValueError("Shape of data and staticmaps do not match")
                data = xr.DataArray(dims=self.dims, data=data, name=name).to_dataset()
            for dvar in data.data_vars.keys():
                if dvar in self._staticmaps:
                    if not self._write:
                        raise IOError(
                            f"Cannot overwrite staticmap {dvar} in read-only mode"
                        )
                    elif self._read:
                        self.logger.warning(f"Overwriting staticmap: {dvar}")
                self._staticmaps[dvar] = data[dvar]

    @property
    def staticgeoms(self):
        """geopandas.GeoDataFrame representation of all model geometries"""
        if not self._staticgeoms:
            if self._read:
                self.read_staticgeoms()
        return self._staticgeoms

    def set_staticgeoms(self, geom, name):
        """Add geom to staticmaps"""
        gtypes = [gpd.GeoDataFrame, gpd.GeoSeries]
        if not np.any([isinstance(geom, t) for t in gtypes]):
            raise ValueError(
                "First parameter map(s) should be geopandas.GeoDataFrame or geopandas.GeoSeries"
            )
        if name in self._staticgeoms:
            if not self._write:
                raise IOError(f"Cannot overwrite staticgeom {name} in read-only mode")
            elif self._read:
                self.logger.warning(f"Overwriting staticgeom: {name}")
        self._staticgeoms[name] = geom

    @property
    def forcing(self):
        """dict of xarray.dataarray representation of all forcing"""
        if not self._forcing:
            if self._read:
                self.read_forcing()
        return self._forcing

    def set_forcing(self, data, name=None):
        """Add data to forcing attribute which is a dictionary of xarray.DataArray.
        The dictionary key is taken from the variable name. In case of a DataArray
        without name, the name can be passed using the optional name argument.

        Arguments
        ---------
        data: xarray.Dataset or xarray.DataArray
            New forcing data to add
        name: str, optional
            Variable name, only in case data is of type DataArray
        """
        # check dataset dtype
        dtypes = [xr.DataArray, xr.Dataset]
        if not np.any([isinstance(data, t) for t in dtypes]):
            raise ValueError("Data type not recognized")
        if isinstance(data, xr.DataArray):
            # NOTE name can be different from data.name !
            if data.name is None and name is not None:
                data.name = name
            elif name is None and data.name is not None:
                name = data.name
            elif data.name is None and name is None:
                raise ValueError("Name required for forcing DataArray.")
            data = {name: data}
        for name in data:
            if name in self._forcing:
                if not self._write:
                    raise IOError(f"Cannot replace forcing {name} in read-only mode")
                self.logger.warning(f"Replacing forcing: {name}")
            self._forcing[name] = data[name]

    @property
    def states(self):
        """dict xarray.dataarray representation of all states"""
        if not self._states:
            if self._read:
                self.read_states()
        return self._states

    def set_states(self, data, name=None):
        """Add data to states attribute which is a dictionary of xarray.DataArray.
        The dictionary key is taken from the variable name. In case of a DataArray
        without name, the name can be passed using the optional name argument.

        Arguments
        ---------
        data: xarray.Dataset or xarray.DataArray
            New forcing data to add
        name: str, optional
            Variable name, only in case data is of type DataArray
        """
        # check dataset dtype
        dtypes = [xr.DataArray, xr.Dataset]
        if not np.any([isinstance(data, t) for t in dtypes]):
            raise ValueError("Data type not recognized")
        if isinstance(data, xr.DataArray):
            # NOTE name can be different from data.name !
            if data.name is None and name is not None:
                data.name = name
            elif name is None and data.name is not None:
                name = data.name
            elif data.name is None and name is None:
                raise ValueError("Name required for forcing DataArray.")
            data = {name: data}
        for name in data:
            if name in self._states:
                if not self._write:
                    raise IOError(f"Cannot replace state {name} in read-only mode")
                self.logger.warning(f"Replacing state: {name}")
            self._states[name] = data[name]

    @property
    def results(self):
        """dict xarray.dataarray representation of model results"""
        if not self._results:
            if self._read:
                self.read_results()
        return self._results

    def set_results(self, data, name=None):
        """Add data to results attribute which is a dictionary of xarray.DataArray.
        The dictionary key is taken from the variable name. In case of a DataArray
        without name, the name can be passed using the optional name argument.

        Arguments
        ---------
        data: xarray.Dataset or xarray.DataArray
            New forcing data to add
        name: str, optional
            Variable name, only in case data is of type DataArray
        """
        # check dataset dtype
        dtypes = [xr.DataArray, xr.Dataset]
        if not np.any([isinstance(data, t) for t in dtypes]):
            raise ValueError("Data type not recognized")
        if isinstance(data, xr.DataArray):
            # NOTE name can be different from data.name !
            if data.name is None and name is not None:
                data.name = name
            elif name is None and data.name is not None:
                name = data.name
            elif data.name is None and name is None:
                raise ValueError("Name required for forcing DataArray.")
            data = {name: data}
        for name in data:
            if name in self._results:
                if not self._write:
                    raise IOError(f"Cannot replace results {name} in read-only mode")
                self.logger.warning(f"Replacing result: {name}")
            self._results[name] = data[name]

    ## properties / methods below can be used directly in actual class

    @property
    def crs(self):
        """Returns coordinate reference system embedded in staticmaps."""
        return self.staticmaps.rio.crs

    def set_crs(self, crs):
        """Embed coordinate reference system staticmaps metadata."""
        return self.staticmaps.rio.set_crs(crs)

    @property
    def dims(self):
        """Returns spatial dimension names of staticmaps."""
        return self.staticmaps.rio.dims

    @property
    def coords(self):
        """Returns coordinates of staticmaps."""
        return self.staticmaps.rio.coords

    @property
    def res(self):
        """Returns coordinates of staticmaps."""
        return self.staticmaps.rio.res

    @property
    def transform(self):
        """Returns spatial transform staticmaps."""
        return self.staticmaps.rio.transform

    @property
    def width(self):
        """Returns width of staticmaps."""
        return self.staticmaps.rio.width

    @property
    def height(self):
        """Returns height of staticmaps."""
        return self.staticmaps.rio.height

    @property
    def shape(self):
        """Returns shape of staticmaps."""
        return self.staticmaps.rio.shape

    @property
    def bounds(self):
        """Returns shape of staticmaps."""
        return self.staticmaps.rio.bounds

    @property
    def region(self):
        """Returns geometry of region of the model area of interest."""
        region = None
        if "region" in self.staticgeoms:
            region = self.staticgeoms["region"]
        elif len(self.staticmaps) > 0:
            crs = None if self.crs is None else self.crs.to_epsg()
            region = gpd.GeoDataFrame(geometry=[box(*self.bounds)], crs=crs)
        return region