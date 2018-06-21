##################################################################
# Copyright 2018 Open Source Geospatial Foundation and others    #
# licensed under MIT, Please consult LICENSE.txt for details     #
##################################################################
from pywps.exceptions import NoApplicableCode

try:
    from osgeo import gdal, ogr
except ImportError as err:
    raise NoApplicableCode('Complex validation requires GDAL/OGR support.')

try:
    import netCDF4
except ImportError as err:
    raise NoApplicableCode('Complex validation requires netCDF4 support.')