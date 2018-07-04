##################################################################
# Copyright 2018 Open Source Geospatial Foundation and others    #
# licensed under MIT, Please consult LICENSE.txt for details     #
##################################################################

from pywps._compat import text_type, StringIO
import os
import requests
import tempfile
import logging
import pywps.configuration as config
from pywps.inout.literaltypes import (LITERAL_DATA_TYPES, convert,
                                      make_allowedvalues, is_anyvalue)
from pywps import get_ElementMakerForVersion, OGCUNIT, NAMESPACES
from pywps.validator.mode import MODE
from pywps.validator.base import emptyvalidator
from pywps.validator import get_validator
from pywps.validator.literalvalidator import (validate_anyvalue,
                                              validate_allowed_values)
from pywps.exceptions import MissingParameterValue, NoApplicableCode, InvalidParameterValue, FileSizeExceeded, \
    StorageNotSupported, FileURLNotSupported

from pywps._compat import PY2, urlparse
import base64
from collections import namedtuple
from io import BytesIO
import shutil

_SOURCE_TYPE = namedtuple('SOURCE_TYPE', 'MEMORY, FILE, STREAM, DATA, URL')
SOURCE_TYPE = _SOURCE_TYPE(0, 1, 2, 3, 4)

LOGGER = logging.getLogger("PYWPS")


def _is_textfile(filename):
    try:
        # use python-magic if available
        import magic
        is_text = 'text/' in magic.from_file(filename, mime=True)
    except ImportError:
        # read the first part of the file to check for a binary indicator.
        # This method won't detect all binary files.
        blocksize = 512
        fh = open(filename, 'rb')
        is_text = b'\x00' not in fh.read(blocksize)
        fh.close()
    return is_text


class UOM(object):
    """
    :param uom: unit of measure
    """

    def __init__(self, uom=''):
        self.uom = uom

    @property
    def json(self):
        return {"reference": OGCUNIT[self.uom],
                "uom": self.uom}


class IOHandlerOld(object):
    """Basic IO class. Provides functions, to accept input data in file,
    memory object and stream object and give them out in all three types

    :param workdir: working directory, to save temporal file objects in
    :param mode: ``MODE`` validation mode


    >>> # setting up
    >>> import os
    >>> from io import RawIOBase
    >>> from io import FileIO
    >>> import types
    >>>
    >>> ioh_file = IOHandler(workdir=tmp)
    >>> assert isinstance(ioh_file, IOHandler)
    >>>
    >>> # Create test file input
    >>> fileobj = open(os.path.join(tmp, 'myfile.txt'), 'w')
    >>> fileobj.write('ASDF ASFADSF ASF ASF ASDF ASFASF')
    >>> fileobj.close()
    >>>
    >>> # testing file object on input
    >>> ioh_file.file = fileobj.name
    >>> assert ioh_file.source_type == SOURCE_TYPE.FILE
    >>> file = ioh_file.file
    >>> stream = ioh_file.stream
    >>>
    >>> assert file == fileobj.name
    >>> assert isinstance(stream, RawIOBase)
    >>> # skipped assert isinstance(ioh_file.memory_object, POSH)
    >>>
    >>> # testing stream object on input
    >>> ioh_stream = IOHandler(workdir=tmp)
    >>> assert ioh_stream.workdir == tmp
    >>> ioh_stream.stream = FileIO(fileobj.name,'r')
    >>> assert ioh_stream.source_type == SOURCE_TYPE.STREAM
    >>> file = ioh_stream.file
    >>> stream = ioh_stream.stream
    >>>
    >>> assert open(file).read() == ioh_file.stream.read()
    >>> assert isinstance(stream, RawIOBase)
    """

    def __init__(self, workdir=None, mode=MODE.NONE):
        self.source_type = None
        self.source = None
        self._tempfile = None
        self.workdir = workdir
        self.uuid = None  # request identifier
        self._stream = None
        self.data_set = False

        self.valid_mode = mode

    def _check_valid(self):
        """Validate this input usig given validator
        """

        validate = self.validator
        _valid = validate(self, self.valid_mode)
        if not _valid:
            self.data_set = False
            raise InvalidParameterValue('Input data not valid using '
                                        'mode %s' % (self.valid_mode))
        self.data_set = True

    def set_file(self, filename):
        """Set source as file name"""
        self.source_type = SOURCE_TYPE.FILE
        self.source = os.path.abspath(filename)
        self._check_valid()

    def set_workdir(self, workdirpath):
        """Set working temporary directory for files to be stored in"""

        if workdirpath is not None and not os.path.exists(workdirpath):
            os.makedirs(workdirpath)

        self._workdir = workdirpath

    def set_memory_object(self, memory_object):
        """Set source as in memory object"""
        self.source_type = SOURCE_TYPE.MEMORY
        self._check_valid()

    def set_stream(self, stream):
        """Set source as stream object"""
        self.source_type = SOURCE_TYPE.STREAM
        self.source = stream
        self._check_valid()

    def set_data(self, data):
        """Set source as simple datatype e.g. string, number"""
        self.source_type = SOURCE_TYPE.DATA
        self.source = data
        self._check_valid()

    def set_url(self, url):
        """Set source as a url."""
        self.source_type = SOURCE_TYPE.URL
        self.source = url
        self._check_valid(self)

    def set_base64(self, data):
        """Set data encoded in base64"""

        self.data = base64.b64decode(data)
        self._check_valid()

    def get_file(self):
        """Get source as file name"""
        if self.source_type == SOURCE_TYPE.FILE:
            return self.source

        elif self.source_type in [SOURCE_TYPE.STREAM, SOURCE_TYPE.DATA, SOURCE_TYPE.URL]:
            if self._tempfile:
                return self._tempfile
            else:
                suffix = ''
                if hasattr(self, 'data_format') and self.data_format.extension:
                    suffix = self.data_format.extension
                (opening, stream_file_name) = tempfile.mkstemp(
                    dir=self.workdir, suffix=suffix)
                openmode = 'w'
                if not PY2 and isinstance(self.source, bytes):
                    # on Python 3 open the file in binary mode if the source is
                    # bytes, which happens when the data was base64-decoded
                    openmode += 'b'
                stream_file = open(stream_file_name, openmode)

                if self.source_type == SOURCE_TYPE.STREAM:
                    stream_file.write(self.source.read())
                elif self.source_type == SOURCE_TYPE.URL:
                    stream_file.write(requests.get(url=self.source, stream=True))
                else:
                    stream_file.write(self.source)

                stream_file.close()
                self._tempfile = str(stream_file_name)
                return self._tempfile

    def get_workdir(self):
        """Return working directory name
        """
        return self._workdir

    def get_memory_object(self):
        """Get source as memory object"""
        # TODO: Soeren promissed to implement at WPS Workshop on 23rd of January 2014
        raise NotImplementedError("setmemory_object not implemented")

    def get_stream(self):
        """Get source as stream object"""
        if self.source_type in [SOURCE_TYPE.FILE, SOURCE_TYPE.URL]:
            if self._stream and not self._stream.closed:
                self._stream.close()
            if self.source_type == SOURCE_TYPE.FILE:
                from io import FileIO
                self._stream = FileIO(self.source, mode='r', closefd=True)
            elif self.source_type == SOURCE_TYPE.URL:
                self._stream = requests.get(url=self.source, stream=True)
            return self._stream

        elif self.source_type == SOURCE_TYPE.STREAM:
            return self.source

        elif self.source_type == SOURCE_TYPE.DATA:
            if not PY2 and isinstance(self.source, bytes):
                return BytesIO(self.source)
            else:
                return StringIO(text_type(self.source))

    def _openmode(self):
        openmode = 'r'
        if not PY2:
            # in Python 3 we need to open binary files in binary mode.
            checked = False
            if hasattr(self, 'data_format'):
                if self.data_format.encoding == 'base64':
                    # binary, when the data is to be encoded to base64
                    openmode += 'b'
                    checked = True
                elif 'text/' in self.data_format.mime_type:
                    # not binary, when mime_type is 'text/'
                    checked = True
            # when we can't guess it from the mime_type, we need to check the file.
            # mimetypes like application/xml and application/json are text files too.
            if not checked and not _is_textfile(self.source):
                openmode += 'b'
        return openmode

    def get_data(self):
        """Get source as simple data object"""

        if self.source_type == SOURCE_TYPE.FILE:
            file_handler = open(self.source, mode=self._openmode())
            content = file_handler.read()
            file_handler.close()
            return content
        elif self.source_type == SOURCE_TYPE.STREAM:
            return self.source.read()
        elif self.source_type == SOURCE_TYPE.DATA:
            return self.source

    @property
    def validator(self):
        """Return the function suitable for validation
        This method should be overridden by class children

        :return: validating function
        """

        return emptyvalidator

    def get_base64(self):
        return base64.b64encode(self.data)

    # Properties
    file = property(fget=get_file, fset=set_file)
    memory_object = property(fget=get_memory_object, fset=set_memory_object)
    stream = property(fget=get_stream, fset=set_stream)
    data = property(fget=get_data, fset=set_data)
    base64 = property(fget=get_base64, fset=set_base64)
    workdir = property(fget=get_workdir, fset=set_workdir)

    def _set_default_value(self, value, value_type):
        """Set default value based on input data type
        """

        if value:
            if value_type == SOURCE_TYPE.DATA:
                self.data = value
            elif value_type == SOURCE_TYPE.MEMORY:
                self.memory_object = value
            elif value_type == SOURCE_TYPE.FILE:
                self.file = value
            elif value_type == SOURCE_TYPE.STREAM:
                self.stream = value


class IOHandler(object):
    """Base IO handling class subclassed by specialized versions: FileHandler, UrlHandler, DataHandler, etc.

    If the specialized handling class is not know when the object is created, instantiate the object with IOHandler.
    The first time the `file`, `url` or `data` attribute is set, the associated subclass will be automatically
    registered. Once set, the specialized subclass cannot be unset.

    Properties
    ----------
    `file` : str
      Filename
    `url` : str
      Link to a resource.
    `data` : object
      A native python object (integer, string, float, etc)
    `stream` : FileIO
      A readable object.



    :param workdir: working directory, to save temporal file objects in
    :param mode: ``MODE`` validation mode


    >>> # setting up
    >>> import os
    >>> from io import RawIOBase
    >>> from io import FileIO
    >>>
    >>> ioh_file = IOHandler(workdir=tmp)
    >>> assert isinstance(ioh_file, IOHandler)
    >>>
    >>> # Create test file input
    >>> fileobj = open(os.path.join(tmp, 'myfile.txt'), 'w')
    >>> fileobj.write('ASDF ASFADSF ASF ASF ASDF ASFASF')
    >>> fileobj.close()
    >>>
    >>> # testing file object on input
    >>> ioh_file.file = fileobj.name
    >>> assert ioh_file.source_type == SOURCE_TYPE.FILE
    >>> file = ioh_file.file
    >>> stream = ioh_file.stream
    >>>
    >>> assert file == fileobj.name
    >>> assert isinstance(stream, RawIOBase)
    >>> # skipped assert isinstance(ioh_file.memory_object, POSH)
    >>>
    >>> # testing stream object on input
    >>> ioh_stream = IOHandler(workdir=tmp)
    >>> assert ioh_stream.workdir == tmp
    >>> ioh_stream.stream = FileIO(fileobj.name,'r')
    >>> assert ioh_stream.source_type == SOURCE_TYPE.STREAM
    >>> file = ioh_stream.file
    >>> stream = ioh_stream.stream
    >>>
    >>> assert open(file).read() == ioh_file.stream.read()
    >>> assert isinstance(stream, RawIOBase)
    """
    prop = None

    def __init__(self, workdir=None, mode=MODE.NONE):
        self._workdir = None
        self.workdir = workdir
        self.valid_mode = mode

        self.inpt = {}
        self._tmpfilename = None
        self._data = None
        self._stream = None
        self._url = None
        self.uuid = None  # request identifier
        self.data_set = False
        self._create_fset_properties()
        self._build_allowed_paths()
        self.as_reference = False

    def _check_valid(self):
        """Validate this input using given validator
        """

        validate = self.validator
        _valid = validate(self, self.valid_mode)
        if not _valid:
            self.data_set = False
            raise InvalidParameterValue('Input data not valid using '
                                        'mode %s'.format(self.valid_mode))
        self.data_set = True

    def _build_allowed_paths(self):
        """
        Store list of allowed paths where files can be stored.

        Note
        ----
        Added /tmp default for testing.

        TODO
        ----
        Review /tmp
        """
        inputpaths = config.get_config_value('server', 'allowedinputpaths') or '/tmp'
        self._allowed_paths = [os.path.abspath(p.strip()) for p in inputpaths.split(':') if p.strip()]

    @property
    def workdir(self):
        return self._workdir

    @workdir.setter
    def workdir(self, path):
        """Set working temporary directory for files to be stored in."""
        if path is not None:
            if not os.path.exists(path):
                os.makedirs(path)

        self._workdir = path

    @property
    def validator(self):
        """Return the function suitable for validation
        This method should be overridden by class children

        :return: validating function
        """

        return emptyvalidator

    @property
    def source_type(self):
        """Return the source type."""
        # For backward compatibility only. source_type checks could be replaced by `isintance`.
        return getattr(SOURCE_TYPE, self.prop.upper())

    @property
    def extension(self):
        """Return the file extension for the data format, if set."""
        if getattr(self, 'data_format', None):
            return self.data_format.extension
        else:
            return ''

    def assign_handler(self, inpt):
        """Subclass with the appropriate handler given the data input."""
        href = inpt.get('href', None)
        self.inpt = inpt

        if href:
            if urlparse(href).scheme == 'file':
                self.file = urlparse(href).path

            else:
                self.url = href
                if inpt.get('method') == 'POST':
                    if 'body' in inpt:
                        data = inpt.get('body')
                    elif 'bodyreference' in inpt:
                        data = requests.get(url=inpt.get('bodyreference')).text
                    else:
                        raise AttributeError("Missing post data content.")

                    self.set_post(data)

        else:
            self.data = inpt.get('data')

    def _build_input_file_name(self, href='', workdir=None):
        """Return a file name for the local system."""
        workdir = workdir or self.workdir or ''
        url_path = urlparse(href).path or ''
        file_name = os.path.basename(url_path).strip() or 'input'
        (prefix, suffix) = os.path.splitext(file_name)
        suffix = suffix or self.extension
        if prefix and suffix:
            file_name = prefix + suffix
        input_file_name = os.path.join(workdir, file_name)
        # build tempfile in case of duplicates
        if os.path.exists(input_file_name):
            input_file_name = tempfile.mkstemp(
                suffix=suffix, prefix=prefix + '_',
                dir=workdir)[1]
        return input_file_name

    def _create_fset_properties(self):
        """Create properties that when set for the first time, will determine
        the instance's handler class.

        Example
        -------
        >>> h = IOHandler()
        >>> isinstance(h, DataHandler)
        False
        >>> h.data = 1 # Mixes the DataHandler class to IOHandler. h inherits DataHandler methods.
        >>> isinstance(h, DataHandler)
        True

        Note that trying to set another attribute (e.g. `h.file = 'a.txt'`) will raise an AttributeError.
        """
        for cls in (FileHandler, DataHandler, StreamHandler, UrlHandler):
            def fset(s, value, kls=cls):
                """Assign the handler class and set the value to the attribute.

                This function will only be called once. The next `fset` will
                use the subclass' property.
                """
                # Add cls methods to this instance.
                extend_instance(s, kls)

                # Set the attribute value through the associated cls property.
                setattr(s, kls.prop, value)

            setattr(IOHandler, cls.prop, property(fget=lambda x: None, fset=fset))


class FileHandler(IOHandler):
    prop = 'file'

    @property
    def file(self):
        """Return filename."""
        return self._tmpfilename

    @file.setter
    def file(self, value):
        """Set file name"""
        self._tmpfilename = self._build_input_file_name(value)
        path = os.path.abspath(value)
        self._validate_file_input(path)
        try:
            os.symlink(path, self._tmpfilename)
            LOGGER.debug("Linked input file %s to %s.", path, self._tmpfilename)
        except NotImplementedError:
            # TODO: handle os.symlink on windows
            LOGGER.warn("Could not link file reference.")
            shutil.copy2(path, self._tmpfilename)

        self._data = None
        self._check_valid()

    @property
    def data(self):
        """Read file and return content."""
        if self._data is None:
            with open(self.file, mode=self._openmode()) as fh:
                self._data = fh.read()
        return self._data

    @property
    def base64(self):
        """Return base64 encoding of data."""
        return base64.b64encode(self.data)

    @property
    def stream(self):
        """Return stream object."""
        from io import FileIO
        if getattr(self, '_stream', None) and not self._stream.closed:
            self._stream.close()

        self._stream = FileIO(self.file, mode='r', closefd=True)
        return self._stream

    @property
    def mem(self):
        """Return memory object."""
        raise NotImplementedError

    @property
    def url(self):
        """Return url to file."""
        import pathlib
        return pathlib.PurePosixPath(self._tmpfilename).as_uri()

    def _openmode(self):
        openmode = 'r'
        if not PY2:
            # in Python 3 we need to open binary files in binary mode.
            checked = False
            if hasattr(self, 'data_format'):
                if self.data_format.encoding == 'base64':
                    # binary, when the data is to be encoded to base64
                    openmode += 'b'
                    checked = True
                elif 'text/' in self.data_format.mime_type:
                    # not binary, when mime_type is 'text/'
                    checked = True
            # when we can't guess it from the mime_type, we need to check the file.
            # mimetypes like application/xml and application/json are text files too.
            if not checked and not _is_textfile(self.file):
                openmode += 'b'
        return openmode

    def _validate_file_input(self, file_path):

        if not file_path:
            raise FileURLNotSupported('Invalid URL path')

        for allowed_path in self._allowed_paths:
            if file_path.startswith(allowed_path):
                LOGGER.debug("Accepted file url as input.")
                return

        raise FileURLNotSupported()


class DataHandler(FileHandler):
    prop = 'data'

    def _mkstemp(self):
        """Return temporary file name."""
        suffix = self.extension
        (opening, fn) = tempfile.mkstemp(dir=self.workdir, suffix=suffix)
        return fn

    @staticmethod
    def _openmode(data):
        openmode = 'w'
        if not PY2 and isinstance(data, bytes):
            # on Python 3 open the file in binary mode if the source is
            # bytes, which happens when the data was base64-decoded
            openmode += 'b'
        return openmode

    @property
    def data(self):
        """Return data."""
        return getattr(self, '_data', None)

    @data.setter
    def data(self, value):
        self._data = value
        self._check_valid()

    @property
    def file(self):
        """Return file name storing the data.

        Requesting the file attributes writes the data to a temporary file on disk.
        """
        if self._tmpfilename is None:
            self._tmpfilename = self._mkstemp()
            with open(self._tmpfilename, self._openmode(self.data)) as fh:
                fh.write(self.data)

        return self._tmpfilename

    @property
    def stream(self):
        """Return a stream representation of the data."""
        if not PY2 and isinstance(self.data, bytes):
            return BytesIO(self.data)
        else:
            return StringIO(text_type(self.data))

    @property
    def url(self):
        """Return a url to a temporary file storing the data."""
        import pathlib
        return pathlib.PurePosixPath(self.file).as_uri()


# Useful ?
class Base64Handler(DataHandler):
    prop = 'base64'

    @property
    def base64(self):
        """Get data encoded in base64."""
        return base64.b64encode(self.data)

    @base64.setter
    def base64(self, value):
        """Set data encoded in base64"""

        self.data = base64.b64decode(value)
        self._check_valid()


class StreamHandler(DataHandler):
    prop = 'stream'

    @property
    def stream(self):
        """Return the stream."""
        return self._stream

    @stream.setter
    def stream(self, value):
        """Set the stream."""
        self._data = None
        self._stream = value
        self._check_valid()

    @property
    def data(self):
        """Return the data from the stream."""
        if self._data is None:
            self._data = self.stream.read()
        return self._data


class UrlHandler(FileHandler):
    prop = 'url'

    @property
    def url(self):
        """Return the URL."""
        return self._url

    @url.setter
    def url(self, value):
        """Set the URL value."""
        self._data = None
        self._url = value
        self._check_valid()
        self.as_reference = True

    @property
    def file(self):
        self._tmpfilename = self._build_input_file_name(href=self.url)

        try:
            reference_file = self._openurl(self.url, self.post_data)
            data_size = reference_file.headers.get('Content-Length', 0)
        except Exception as e:
            raise NoApplicableCode('File reference error: %s' % e)

        # If the response did not return a 'Content-Length' header then
        # calculate the size
        if data_size == 0:
            LOGGER.debug('No Content-Length in header, calculating size.')

        # Check if input file size was not exceeded
        max_byte_size = self.max_input_size()

        FSEE = FileSizeExceeded(
            'File size for input {} exceeded. Maximum allowed: {} megabytes'.
            format(getattr(self.inpt, 'identifier', '?'), max_byte_size))

        if int(data_size) > int(max_byte_size):
            raise FSEE
        try:
            with open(self._tmpfilename, 'wb') as f:
                data_size = 0
                for chunk in reference_file.iter_content(chunk_size=1024):
                    data_size += len(chunk)
                    if int(data_size) > int(max_byte_size):
                        raise FSEE

                    f.write(chunk)
        except Exception as e:
            raise NoApplicableCode(e)

        return self._tmpfilename

    @property
    def post_data(self):
        return getattr(self, '_post_data', None)

    @post_data.setter
    def post_data(self, value):
        self._post_data = value

    @staticmethod
    def _openurl(href, data=None):
        """Open given href.
        """
        LOGGER.debug('Fetching URL %s', href)
        if data is not None:
            req = requests.post(url=href, data=data, stream=True)
        else:
            req = requests.get(url=href, stream=True)

        return req

    @staticmethod
    def max_input_size():
        """Calculates maximal size for input file based on configuration
        and units

        :return: maximum file size in bytes
        """
        ms = config.get_config_value('server', 'maxsingleinputsize')
        return config.get_size_mb(ms) * 1024**2


class LiteralHandler(DataHandler):
    """Data handler for Literal In- and Outputs

    >>> class Int_type(object):
    ...     @staticmethod
    ...     def convert(value): return int(value)
    >>>
    >>> class MyValidator(object):
    ...     @staticmethod
    ...     def validate(inpt): return 0 < inpt.data < 3
    >>>
    >>> inpt = SimpleHandler(data_type = Int_type)
    >>> inpt.validator = MyValidator
    >>>
    >>> inpt.data = 1
    >>> inpt.validator.validate(inpt)
    True
    >>> inpt.data = 5
    >>> inpt.validator.validate(inpt)
    False
    """

    def __init__(self, workdir=None, data_type='integer', mode=MODE.NONE):
        DataHandler.__init__(self, workdir=workdir, mode=mode)
        if data_type not in LITERAL_DATA_TYPES:
            raise ValueError('data_type {} not in {}'.format(data_type, LITERAL_DATA_TYPES))
        self.data_type = data_type

    @DataHandler.data.setter
    def data(self, value):
        """Set data value. Inputs are converted into target format.
        """
        if self.data_type and value is not None:
            value = convert(self.data_type, value)

        DataHandler.data.fset(self, value)


class BasicIO:
    """Basic Input/Output class
    """
    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 min_occurs=1, max_occurs=1, metadata=[]):
        self.identifier = identifier
        self.title = title
        self.abstract = abstract
        self.keywords = keywords
        self.min_occurs = int(min_occurs)
        self.max_occurs = int(max_occurs)
        self.metadata = metadata


class BasicLiteral:
    """Basic literal Input/Output class
    """

    def __init__(self, uoms=None):
        # list of uoms
        self.uoms = []
        # current uom
        self._uom = None

        # add all uoms (upcasting to UOM)
        if uoms is not None:
            for uom in uoms:
                if not isinstance(uom, UOM):
                    uom = UOM(uom)
                self.uoms.append(uom)

        if self.uoms:
            # default/current uom
            self.uom = self.uoms[0]

    @property
    def uom(self):
        return self._uom

    @uom.setter
    def uom(self, uom):
        if uom is not None:
            self._uom = uom


class BasicComplex(object):
    """Basic complex input/output class

    """

    def __init__(self, data_format=None, supported_formats=None):
        self._data_format = data_format
        self._supported_formats = None
        if supported_formats:
            self.supported_formats = supported_formats
        if self.supported_formats:
            # not an empty list, set the default/current format to the first
            self.data_format = supported_formats[0]

    def get_format(self, mime_type):
        """
        :param mime_type: given mimetype
        :return: Format
        """

        for frmt in self.supported_formats:
            if frmt.mime_type == mime_type:
                return frmt
        else:
            return None

    @property
    def validator(self):
        """Return the proper validator for given data_format
        """

        return self.data_format.validate

    @property
    def supported_formats(self):
        return self._supported_formats

    @supported_formats.setter
    def supported_formats(self, supported_formats):
        """Setter of supported formats
        """

        def set_format_validator(supported_format):
            if not supported_format.validate or \
               supported_format.validate == emptyvalidator:
                supported_format.validate =\
                    get_validator(supported_format.mime_type)
            return supported_format

        self._supported_formats = list(map(set_format_validator, supported_formats))

    @property
    def data_format(self):
        return self._data_format

    @data_format.setter
    def data_format(self, data_format):
        """self data_format setter
        """
        if self._is_supported(data_format):
            self._data_format = data_format
            if not data_format.validate or data_format.validate == emptyvalidator:
                data_format.validate = get_validator(data_format.mime_type)
        else:
            raise InvalidParameterValue("Requested format "
                                        "%s, %s, %s not supported" %
                                        (data_format.mime_type,
                                         data_format.encoding,
                                         data_format.schema),
                                        'mimeType')

    def _is_supported(self, data_format):

        if self.supported_formats:
            for frmt in self.supported_formats:
                if frmt.same_as(data_format):
                    return True

        return False


class BasicBoundingBox(object):
    """Basic BoundingBox input/output class
    """

    def __init__(self, crss=None, dimensions=2):
        self.crss = crss or ['epsg:4326']
        self.crs = self.crss[0]
        self.dimensions = dimensions
        self.ll = []
        self.ur = []


class LiteralInput(BasicIO, BasicLiteral, LiteralHandler):
    """LiteralInput input abstract class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 data_type="integer", workdir=None, allowed_values=None,
                 uoms=None, mode=MODE.NONE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None, default_type=SOURCE_TYPE.DATA):
        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs=min_occurs, max_occurs=max_occurs, metadata=metadata)
        BasicLiteral.__init__(self, uoms)
        LiteralHandler.__init__(self, workdir, data_type, mode=mode)

        if default_type != SOURCE_TYPE.DATA:
            raise InvalidParameterValue("Source types other than data are not supported.")

        self.any_value = is_anyvalue(allowed_values)
        self.allowed_values = []
        if not self.any_value:
            self.allowed_values = make_allowedvalues(allowed_values)

        if default is not None:
            self.data = default

    @property
    def validator(self):
        """Get validator for any value as well as allowed_values
        :rtype: function
        """

        if self.any_value:
            return validate_anyvalue
        else:
            return validate_allowed_values

    @property
    def json(self):
        """Get JSON representation of the input
        """
        data = {
            'identifier': self.identifier,
            'title': self.title,
            'abstract': self.abstract,
            'keywords': self.keywords,
            'type': 'literal',
            'data_type': self.data_type,
            'workdir': self.workdir,
            'any_value': self.any_value,
            'allowed_values': [value.json for value in self.allowed_values],
            'mode': self.valid_mode,
            'data': self.data,
            'min_occurs': self.min_occurs,
            'max_occurs': self.max_occurs
        }
        if self.uoms:
            data["uoms"] = [uom.json for uom in self.uoms],
        if self.uom:
            data["uom"] = self.uom.json
        return data


class LiteralOutput(BasicIO, BasicLiteral, LiteralHandler):
    """Basic LiteralOutput class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 data_type=None, workdir=None, uoms=None, validate=None,
                 mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        BasicLiteral.__init__(self, uoms)
        LiteralHandler.__init__(self, workdir=workdir, data_type=data_type, mode=mode)

        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage

    @property
    def validator(self):
        """Get validator for any value as well as allowed_values
        """
        return validate_anyvalue


class BBoxInput(BasicIO, BasicBoundingBox, DataHandler):
    """Basic Bounding box input abstract class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=[], crss=None,
                 dimensions=None, workdir=None,
                 mode=MODE.SIMPLE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None, default_type=SOURCE_TYPE.DATA):
        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs, max_occurs, metadata)
        BasicBoundingBox.__init__(self, crss, dimensions)
        DataHandler.__init__(self, workdir=workdir, mode=mode)

        if default_type != SOURCE_TYPE.DATA:
            raise InvalidParameterValue("Source types other than data are not supported.")

        self.default = default

    @property
    def json(self):
        """Get JSON representation of the input. It returns following keys in
        the JSON object:

            * identifier
            * title
            * abstract
            * type
            * crs
            * bbox
            * dimensions
            * workdir
            * mode
        """
        return {
            'identifier': self.identifier,
            'title': self.title,
            'abstract': self.abstract,
            'keywords': self.keywords,
            'type': 'bbox',
            'crs': self.crs,
            'crss': self.crss,
            'bbox': (self.ll, self.ur),
            'dimensions': self.dimensions,
            'workdir': self.workdir,
            'mode': self.valid_mode,
            'min_occurs': self.min_occurs,
            'max_occurs': self.max_occurs
        }


class BBoxOutput(BasicIO, BasicBoundingBox, DataHandler):
    """Basic BoundingBox output class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None, crss=None,
                 dimensions=None, workdir=None, mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        BasicBoundingBox.__init__(self, crss, dimensions)
        DataHandler.__init__(self, workdir=workdir, mode=mode)
        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage


class ComplexInput(BasicIO, BasicComplex, IOHandler):
    """Complex input abstract class

    >>> ci = ComplexInput()
    >>> ci.validator = 1
    >>> ci.validator
    1
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 workdir=None, data_format=None, supported_formats=None,
                 mode=MODE.NONE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None):

        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs, max_occurs, metadata)
        IOHandler.__init__(self, workdir=workdir, mode=mode)
        BasicComplex.__init__(self, data_format, supported_formats)

        if default is not None:
            self.data = default

    @property
    def json(self):
        """Get JSON representation of the input
        """
        return {
            'identifier': self.identifier,
            'title': self.title,
            'abstract': self.abstract,
            'keywords': self.keywords,
            'type': 'complex',
            'data_format': self.data_format.json,
            'supported_formats': [frmt.json for frmt in self.supported_formats],
            'file': self.file,
            'workdir': self.workdir,
            'mode': self.valid_mode,
            'min_occurs': self.min_occurs,
            'max_occurs': self.max_occurs
        }


class ComplexOutput(BasicIO, BasicComplex, IOHandler):
    """Complex output abstract class

    >>> # temporary configuration
    >>> import ConfigParser
    >>> from pywps.storage import *
    >>> config = ConfigParser.RawConfigParser()
    >>> config.add_section('FileStorage')
    >>> config.set('FileStorage', 'target', './')
    >>> config.add_section('server')
    >>> config.set('server', 'outputurl', 'http://foo/bar/filestorage')
    >>>
    >>> # create temporary file
    >>> tiff_file = open('file.tiff', 'w')
    >>> tiff_file.write("AA")
    >>> tiff_file.close()
    >>>
    >>> co = ComplexOutput()
    >>> co.file = 'file.tiff'
    >>> fs = FileStorage(config)
    >>> co.storage = fs
    >>>
    >>> url = co.get_url() # get url, data are stored
    >>>
    >>> co.stream.read() # get data - nothing is stored
    'AA'
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 workdir=None, data_format=None, supported_formats=None,
                 mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        IOHandler.__init__(self, workdir=workdir, mode=mode)
        BasicComplex.__init__(self, data_format, supported_formats)

        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage

    def get_url(self):
        """Return URL pointing to data
        """
        (outtype, storage, url) = self.storage.store(self)
        return url


def extend_instance(obj, cls):
    """Apply mixins to a class instance after creation"""
    base_cls = obj.__class__
    base_cls_name = obj.__class__.__name__
    obj.__class__ = type(base_cls_name, (cls, base_cls), {})


if __name__ == "__main__":
    import doctest
    from pywps.wpsserver import temp_dir

    with temp_dir() as tmp:
        os.chdir(tmp)
        doctest.testmod()
