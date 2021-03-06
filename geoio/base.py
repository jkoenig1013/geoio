'''
High level geo file image access

Base GeoImage class is a wrapper around gdal providing easy access to
image metadata, file read/write, and i/o.  Higher level classes (such
as DGImage) can build on this class to provide data specific metadata and
spectral transformations.
'''

from __future__ import division

from osgeo import gdal, gdalconst, osr, ogr
import numpy as np
import os
import warnings
import collections
import textwrap
import tempfile
import logging
import math
from tzwhere import tzwhere
import tinytools as tt

# package import
import constants as const

# Module setup
gdal.UseExceptions()
ogr.UseExceptions()
logger = logging.getLogger(__name__)
# To get access to logging statements from the command line:
# import logging
# logging.basicConfig(level=logging.DEBUG) # or your desired level


class OverlapError(ValueError):
    '''Raise when the window does not overlap the image.  This can be
    caught and passed when the window is expected to not overlap in
    some cases.
    '''
    pass


class GeoImage(object):
    """
    Base image class providing high-level access to image data and metadata
    as well as methods to easily interact with the image and related
    vector files.

    Functionality is tested against .TIL, .VRT, and .TIF formats, but this
    base class should support any GDAL format.

    Parameters
    ----------
    file_in : str
        String describing a file on disk that is of a valid input format.
    derived_dir : str
        The location to store files created by the class.

    Attributes
    ----------
    files : tinytools.bunch.OrderedBunch
        A collection of the image files used in the object.  The GeoImage
        class populates:

        -derived_dir : The directory into which create files are stored.
        -dfile : The input image file.
        -dfile_tiles : The tiles of the virtual data set - if the data set
                       doesn't contain tiles, this should be equal to dfile.

        Inherited classes can override/extend this listing.
    meta : tinytools.bunch.OrderedBunch
        Summary metadata of the base image.
    shape : tuple
        shape of the image in gdal format (bands,x,y).
    resolutions : tuple
        length 2 tuple with resolutions of x and y image dimensions.
    """

    def __init__(self, file_in, derived_dir=None):
        """Initialize class with data and meta-data from file.  __init__
        class is in the class definition. """

        ## Search for files that are needed
        if not os.path.isfile(file_in):
            raise ValueError("The file that was passed in does not exist.")

        ## Start populating files dictionary for geoimage info
        # ... variables created here:
        # self.files_dict['fin']
        # self.files_dict['gdal_file_list']
        # self.files_dict['dfile']
        # self.files_dict['dfile_tiles']
        # self.files_dict['derived_dir']

        # Get the file name and full file directory
        ifile = os.path.abspath(file_in)
        fname = os.path.basename(ifile)
        fdir = os.path.dirname(ifile)
        flist = os.listdir(fdir)

        # Create files dictionary to populate - this will be bunched later
        #!# self.files_dict = {}
        self.files = tt.bunch.OrderedBunch({})

        # Set the place to store/retrive derived files (i.e. spectral data)
        if derived_dir:
            if not os.path.isdir(derived_dir):
                raise ValueError("The requested derived data directory does "
                                 "not exist.")
            if not os.access(os.path.join(derived_dir, ''), os.W_OK):
                raise ValueError("Write access is required for the requested "
                                 "location passed into derived_dir.")
            self.files.derived_dir = os.path.join(derived_dir, '')
        else:
            if os.access(fdir, os.W_OK):
                self.files.derived_dir = fdir
            else:
                self.files.derived_dir = fdir
                warnings.warn("The input file location is not writable.  "
                              "Derived file creation (i.e. spectral files) "
                              "will not be available. Either write permissions "
                              "need to be provided or the object can be "
                              "reinstantiated with a writable location passed "
                              "to the input variable dervied_store_dir.")

        ### Setup the dataset and subdataset variables
        (tmpfile,tmptiles)=self._get_file_and_tiles(ifile)

        #!# self.files_dict['dfile'] = tmpfile
        #!# self.files_dict['dfile_tiles'] = tmptiles
        self.files.dfile = tmpfile
        self.files.dfile_tiles = tmptiles

        # Create the files dictionary bunch
        #!#self.files = tt.bunch.OrderedBunch(self.files_dict)

        ## Populate geoimage info
        # ... variables created here:
        # self._fobj
        # self.meta_geoimg_dict
        # self.meta

        # Open the image in files.dfile
        self._fobj = self._get_gdal_obj(self.files.dfile,
                                        self.files.dfile_tiles)

        # Populate metadata info from gdal
        self._set_metadata()


    def _get_file_and_tiles(self, ifile):

        # If fname is a .til file then create .vrt
        if tt.files.filter(ifile, '*.TIL', case_sensitive=False):
            file_loc = ifile
            tiles_loc = tt.pvl.read_from_pvl(ifile,'filename')
            dname = os.path.dirname(ifile)
            tiles_loc = [os.path.join(dname,x) for x in tiles_loc]

        # If fname is a VRT, pull subdatasets from the file object
        elif tt.files.filter(ifile, '*.VRT', case_sensitive=False):
            file_loc = ifile
            tmp = gdal.Open(file_loc) # Open to pull VRT file list
            tmp_files = tmp.GetFileList()
            tiles_loc = [x for x in tmp_files if not
                            tt.files.filter(x, '*.VRT', case_sensitive=False)]
            tmp = None # Close the opened file from above

        # If this is an ENVI file, then a file without an extension should
        # exist and will be the root file.
        elif os.path.isfile(os.path.splitext(ifile)[0]):
            tmp = os.path.splitext(ifile)[0]
            file_loc = tmp
            tiles_loc = [tmp]

        # If file input isn't a .TIL, .VRT, or ENVI file, just pass it into the
        # object vars to pass to gdal.
        else:
            # Else, just pass it on to open in gdal
            file_loc = ifile
            tiles_loc = [ifile]

        return (file_loc,tiles_loc)


    def _get_gdal_obj(self, dfile, dfile_tiles):
        '''Return gdal object for the GeoImage.'''

        # Need to handle .TIL files specifically because gdal does not fully
        # support them.
        if tt.files.filter(dfile, '*.TIL', case_sensitive=False):
            # If this is a .TIL file, then create a VRT, copy it to an
            # "in memory" obj and then remove the VRT file from disk.

            # Get a temporary file
            file_temp = tempfile.NamedTemporaryFile(suffix=".VRT")

            # Build the vrt command
            cmd = []
            cmd.append("gdalbuildvrt")
            cmd.append(file_temp.name)
            for i in dfile_tiles: cmd.append(i)

            # Execute the buildvrt command
            tt.cmd_line.exec_cmd(cmd)
            # gdal does not issue errors to the command line,
            # so try/except on tt.cmd_line.exec_cmd won't work...
            # Check that file exists to see if the buildvrt succeeded.
            if not os.path.isfile(file_temp.name):
                raise StandardError("Creation of file " + file_temp.name + " "
                                    "failed. This could possibly be a "
                                    "write access problem?")

            vvv = gdal.Open(file_temp.name)

            # Create MEM copy of VRT - created "in memory" by passing an empty
            # file name string to the VRT driver.  If I used the "MEM" driver,
            # the full dataset would be read into memory on creation.
            drv = gdal.GetDriverByName('VRT')
            obj = drv.CreateCopy('',vvv)

            # Delete VRT
            file_temp.close()

            if os.path.isfile(file_temp.name):
                raise StandardError("Removal of file " + file_temp.name + " "
                                    "failed. There is something wrong with "
                                    "the .TIL handling.")

        else:
            obj = gdal.Open(self.files.dfile, gdalconst.GA_ReadOnly)

        return obj


    def _set_metadata(self):
        """ Get image metadata."""
        meta_geoimg_dict = read_geo_file_info(self._fobj)

        # Need to handle case of a .TIL that results an in memory VRT.
        # In this case, file_name will be the VRT string when returned
        # from the gdal driver above and does not print well or return the
        # intended information.
        if not os.path.isfile(meta_geoimg_dict['file_name']):
            meta_geoimg_dict['file_name'] = self.files.dfile

        # Add geoio class name to dictionary
        meta_geoimg_dict['class_name'] = self.__class__.__name__

        ### OrderedBunch the metadata from the read_geo_file_info dictionary
        self.meta = tt.bunch.OrderedBunch(meta_geoimg_dict)

        # Set class members
        self.shape = self.meta.shape
        self.resolution = self.meta.resolution


    def __repr__(self):
        """Human readable image summary similar to the R package 'raster'."""
        sss = ''
        su = self.meta

        prefixes = collections.OrderedDict()
        prefixes['Class Name'] = (['class_name'],'')
        prefixes['Driver Name'] = (['driver_name'],'')
        prefixes['Data Type'] = (['gdal_dtype_name'],'')
        prefixes['File Name'] = (['file_name'],'')
        prefixes['File List'] = (['file_list'], '')
        prefixes['Dimensions'] = (['shape'],
                                  ' (nlayers, nrows, ncols)')
        prefixes['Resolution'] = (['resolution'],' (x,y)')
        # The following is inverted because xstart, xend, etc are calculated
        # in pixel space and this is inverted from the North = max, South = min
        # paradigm.
        prefixes['Extent'] = (['extent'],' (xmin, xmax, ymin, ymax)')
        prefixes['Projection String'] = (['pprint_proj_string'],'')
        prefixes['Geo Transform'] = (['geo_transform'],'')
        prefixes['Authority'] = (['authority'], '')

        ### Loop through prefixes and su to print data to screen
        # Gen max length of labels to set prefix length
        prelen = max([len(x) for x in prefixes])
        # Loop through each prefix to put together wrapped string
        for x in prefixes:
            prefix = x+' '*(prelen-len(x))+' : '
            width_set = 80
            wrapper = textwrap.TextWrapper(initial_indent=prefix,
                                           width=width_set,
                                           replace_whitespace=False,
                                           subsequent_indent=' '*len(prefix))

            message = ', '.join([str(su[y]) for y in prefixes[x][0]])
            message = message+prefixes[x][1]

            # Handle different message formats:
            # If message contains new lines, just pass those through to print.
            if message.find('\n') != -1:
                sss = sss + prefix + message.replace('\n','\n'+' '*prelen)+'\n'
            # Else if message is not empty, pass to wrapper.fill
            elif message:
                sss = sss + wrapper.fill(message) + '\n'
            # Else just pass the empty message along with prefix
            else:
                sss = sss + prefix + '\n'

        return sss


    def print_img_summary(self):
        """Echo the object's __repr__ method."""
        print(self.__repr__())


    def __iter__(self):
        '''Yield from default iter_window iterator.'''
        for x in self.iter_window():
            yield x


    def iter_base(self, xoff, yoff, win_xsize, win_ysize, **kwargs):
        '''
        Base iterator function to yield data from array-like window parameters.

        Parameters
        ----------
        xoff : array_like
            x offset(s) for the image regions to be read.
        yoff : array_like
            y offset(s) for the image regions to be read.
        win_xsize : array_like
            window x-dim size(s) for the image regions to be read.
        win_ysize : array_like
            window y-dim size(s) for the image regions to be read.
        kwargs : optional
            keyword arguments to be passed to get_data.

        Yields
        ------
        ndarray
            Three dimensional numpy array of data from the requested region
            of the image.

        '''

        logger.debug('*** begin iter_base ***')

        # Broadcast array_like
        windows = np.broadcast(xoff,yoff,win_xsize,win_ysize)

        # Iterate through windows generated from input parameters
        for w in windows:
            logger.debug('window parameters: xoff %s, yoff %s, '
                                            'win_xsize %s, win_ysize %s',
                                             w[0], w[1], w[2], w[3])
            yield self.get_data(window=w,**kwargs)


    def iter_window(self, win_size=None, stride=None, **kwargs):
        '''
        Window iterator that yields data from the image based on win_size
        and stride.

        win_size and stride are both optional arguments.  If neither are
        passed, then the method pulls win_size from GDAL GetBlockSize() and
        uses that to step through the image.  If only win_size is provided,
        the method yields adjoining windows of the size requested.  If only
        stride is provided, an error is rasied.

        Parameters
        ----------
        win_size : array-like, length 2, optional
            The size of the requested image chip in x and y.
        stride : array-like, length 2, optional
            The size of the step between each yielded chip in x and y.
        kwargs: optional
            Arguments for get_data().

        Yields
        ------
        ndarray
            Three dimensional numpy array of pixel values from the
            requested region of the image.

        '''

        logger.debug('*** begin iter_window ***')

        # Check input values
        if win_size:
            if any(x <= 0 for x in win_size):
                raise ValueError('No value in win_size can be equal '
                                 'to or less than zero.')

        if stride:
            if any(x <= 0 for x in stride):
                raise ValueError('No value in stride can be equal '
                                 'to or less than zero.')

        # if NOT win_size and NOT stride
        # use gdal to figure out block size and then continue on below.
        if not win_size and not stride:
            # Get block size from gdal
            b = self._fobj.GetRasterBand(1)
            win_size = b.GetBlockSize()

        logger.debug('win_size is:  %s, stride is:  %s', win_size, stride)

        # if win_size and NOT stride
        # set stride to make windows adjoining
        if win_size and not stride:
            # Set vars for easy access below
            xs = self.meta.shape[1]
            ys = self.meta.shape[2]
            xsize, ysize = win_size

            # Find starting offsets by identifying the pixels that don't fit in
            # the requested window blocks and then split the different
            # between ends of the image using floor (int) to reduce fractions.
            x_extra_pixels = xs % win_size[0]
            xoff = int(x_extra_pixels / 2.0)
            y_extra_pixels = ys % win_size[1]
            yoff = int(y_extra_pixels / 2.0)

            # Use while True to loop through get_data until outside the image
            xoff_start = xoff
            xsize, ysize = win_size
            while True:
                logger.debug(' xoff is %s,\tyoff is %s', xoff, yoff)
                yield self.get_data(window=[xoff, yoff, xsize, ysize],**kwargs)
                xoff += xsize
                if xoff > self.meta.shape[1]:
                    xoff = xoff_start
                    yoff += ysize
                if yoff > self.meta.shape[2]:
                    break

        # if NOT win_size and stride, raise error
        elif not win_size and stride:
            raise ValueError('Setting stride and not setting win_size is not '
                             'allowed because there is no resonable value to '
                             'set win_size to.  In this case stride can be '
                             'even or odd which could result in alternative '
                             'size return blocks around the center pixel '
                             '(or fractional pixel).')

        # if win_size and stride
        # just do it
        elif win_size and stride:
            # Set vars for easy access below
            xs = self.meta.shape[1]
            ys = self.meta.shape[2]
            xsize, ysize = win_size
            xstride, ystride = stride

            # Find starting offset by identifying pixels that don't fit in
            # the requested size/stride and then split the different between
            # ends of the image using floor (int) to reduce fractions.
            x_extra_pixels = (xs - xsize) % xstride
            xoff = int(x_extra_pixels/2.0)
            y_extra_pixels = (ys - ysize) % ystride
            yoff = int(y_extra_pixels/2.0)

            # Start the yield loop
            xoff_start = xoff
            while True:
                logger.debug(' xoff is %s,\tyoff is %s', xoff, yoff)
                yield self.get_data(window=[xoff, yoff, xsize, ysize], **kwargs)
                xoff += xstride
                if xoff > self.meta.shape[1]:
                    xoff = xoff_start
                    yoff += ystride
                if yoff > self.meta.shape[2]:
                    break


    def iter_window_random(self, win_size=None, no_chips=1000, **kwargs):
        """Random chip iterator.

        Parameters
        ----------
        win_size : array-like, length 2
            The size of the requested image chip in x and y.
        no_chips : int, optional
            Number of chips to generate.
        kwargs: optional
            Arguments for get_data().

        Yields
        ------
        ndarray
            Three dimensional numpy array of pixel values from the
            requested region of the image.
        """

        # Check input values
        if win_size:
            if any(x <= 0 for x in win_size):
                raise ValueError('No value in win_size can be equal '
                                 'to or less than zero.')

        counter = no_chips
        xs = self.meta.shape[1]
        ys = self.meta.shape[2]
        xsize, ysize = win_size

        while True:
            # select random offset
            xoff = np.random.randint(xs-xsize+1)
            yoff = np.random.randint(ys-ysize+1)
            yield self.get_data(window=[xoff, yoff, xsize, ysize], **kwargs)
            counter -= 1
            if counter == 0: break


    def iter_components(self, **kwargs):
        """This is a convenience method that iterataes (via yield) through
        the components in the image object.  Any kwargs valid for get_data
        can be passed through."""

        for c in xrange(len(self.files.dfile_tiles)):
            yield self.get_data(component=c, **kwargs)


    def iter_vector(self, vector=None, properties=False, filter=None, **kwargs):
        """This method iterates (via yeild) through a vector object or file.
        Any kwargs valid for get_data can be passed through."""

        if 'window' in kwargs.keys():
            raise ValueError("The window argument is not valid for this " \
                             "method. They both define a retrieval " \
                             "geometry.  Pass one or the other.")

        if 'geom' in kwargs.keys():
            raise ValueError("The geom argument is not valid for this " \
                             "method. The vector file passed in defines " \
                             "the retrieval geometry.")

        # ToDo Test for overlap of geom and image data?

        obj = ogr.Open(vector)
        lyr = obj.GetLayer(0)
        lyr_sr = lyr.GetSpatialRef()

        img_proj = self.meta.projection_string
        img_trans = self.meta.geo_transform
        img_sr = osr.SpatialReference()
        img_sr.ImportFromWkt(img_proj)

        coord_trans = osr.CoordinateTransformation(lyr_sr, img_sr)

        for feat in lyr:
            # Return feature properties data is requested
            if properties is True:
                prop_out = feat.items()
            elif properties:
                if isinstance(properties, (list, tuple, str)):
                    if isinstance(properties, str):
                        properties = [properties]
                    it = feat.items()
                    if not all(x for x in properties if x in it.keys()):
                        raise ValueError("One or more of the requested "
                                         "properties are not in the vector "
                                         "feature.")
                    prop_out = {x: it[x] for x in properties if x in it.keys()}
                    if not prop_out:
                        prop_out = None
                        warnings.warn("No properties value found matching "
                                      "request.")
                else:
                    raise ValueError("Invalid properties argument.")


            # Determine if the feature should be returned based on value of
            # filter and if the value exists in the feature properties.
            if filter:
                # The filter should be either a list of dictionary key/value
                # paris of length one, or of list of key/value pairs.  The
                # idea is that you can filter against more than one value
                # of a key when you can pass a list of pairs.
                if isinstance(filter, dict) & (len(filter) != 1):
                    raise ValueError("Filters should be passed in as a " \
                                     "list of dictionaries that will " \
                                     "be used to filter against the " \
                                     "feature property values.")

                # If filter is a dict of len 1, convert to a list of len 1 for
                # the looping code below.
                if isinstance(filter,dict):
                    filter = [filter]

                # Get feature properties to check against.
                prop_test = feat.items()

                # raise warning if a filter item key is not in properties
                if any(f.keys()[0] not in prop_test.keys() for f in filter):
                    warnings.warn("Requested filter key is not present in "
                                  "vector properties.")

                # If any filter is caught, pass on, otherwise return
                if any(prop_test.get(d.keys()[0], None) ==
                                    d.values()[0] for d in filter):
                    pass
                else:
                    if properties:
                        yield (None, None)
                        continue
                    else:
                        yield None
                        continue

            # Get the geometry to pass to get_data
            geom = feat.geometry()

            # Use transform from above to put geom in image space
            geom.Transform(coord_trans)

            # Catch and pass OverlapError for the iterator
            try:
                data = self.get_data(geom=geom, **kwargs)
            except OverlapError:
                data = None

            # Yield the data
            if properties:
                yield (data, prop_out)
            else:
                yield data


    def get_data_from_vec_extent(self, vector=None, **kwargs):
        """This is a convenience method to find the extent of a vector and
        return the data from that extent.  kwargs can be anything accepted
        by get_data."""
        if vector is None:
            raise ValueError("Requires a vector to read.  The vector can be " \
                             "a string that describes a vector object or a " \
                             "path to a valid vector file.")

        if 'window' in kwargs.keys():
            raise ValueError("The window argument is not valid for this " \
                             "method. The vector file passed in defines " \
                             "the retrieval geometry.")

        if 'geom' in kwargs.keys():
            raise ValueError("The geom argument is not valid for this " \
                             "method. The vector file passed in defines " \
                             "the retrieval geometry.")

        if 'mask' in kwargs.keys():
            raise ValueError("A mask request is not valid for this method " \
                             "because it retrives data from the full extent " \
                             "of the vector.  You might want a rasterize " \
                             "method or iter_vector?")

        # ToDo Test for overlap of geom and image data?

        obj = ogr.Open(vector)
        lyr = obj.GetLayer(0)
        lyr_sr = lyr.GetSpatialRef()

        img_proj = self.meta.projection_string
        img_trans = self.meta.geo_transform
        img_sr = osr.SpatialReference()
        img_sr.ImportFromWkt(img_proj)

        coord_trans = osr.CoordinateTransformation(lyr_sr,img_sr)

        extent = lyr.GetExtent()

        window = self._extent_to_window(extent,coord_trans)
        [xoff, yoff, win_xsize, win_ysize] = window

        return self.get_data(window = window, **kwargs)


    def _extent_to_window(self,extent,coord_trans=None):

        if not coord_trans:
            warnings.warn('No projection checking is done.  Returning passed '
                          'geometry in image space.')

        [minX, maxX, minY, maxY] = extent
        ul_vec = [minX, maxY]
        lr_vec = [maxX, minY]

        if coord_trans:
            # Online dobumentation says that there could be a bug in
            # TransformPoints - seems to be working with 1.11.4 so
            # I'll leave it in for now.  When working with a bag vector file
            # TransformPoints returned inf values that exploded below whereas
            # the sequental TransformPoint returned values that triggered
            # the overlap error below.  Leaving the single call for now
            # as it is faster and cleaner.
            [ul_img, lr_img] = coord_trans.TransformPoints([ul_vec, lr_vec])
            #ul_img = coord_trans.TransformPoint(*ul_vec)
            #lr_img = coord_trans.TransformPoint(*lr_vec)

            # Cut off third returned dimension (should be zero)
            ul_img = ul_img[:-1]
            lr_img = lr_img[:-1]
        else:
            ul_img = ul_vec
            lr_img = lr_vec

        xs,ys = self.proj_to_raster(*zip(*[ul_img,lr_img]))

        xoff = int(math.floor(min(xs)))
        yoff = int(math.floor(min(ys)))

        xmax = int(math.ceil(max(xs)))
        ymax = int(math.ceil(max(ys)))

        win_xsize = xmax-xoff
        win_ysize = ymax-yoff

        window = [xoff, yoff, win_xsize, win_ysize]

        # Logging info if needed
        logger.debug('vector geo extent...\n\t%s\n\t%s',ul_vec,lr_vec)
        logger.debug('image geo extent...\n\t%s\n\t%s',ul_img,lr_img)
        logger.debug('geo transform...\n\t%s', self.meta.geo_transform)
        logger.debug('raster xy extent...\n\t%s,\n\t%s',xs,ys)
        logger.debug('requested window...\n\t%s',window)

        if ((xoff + win_xsize <= 0) or (yoff + win_ysize <= 0) or
            (xoff > self.meta.shape[1]) or (yoff > self.meta.shape[2])):
            raise OverlapError("The requested data window has no " \
                              "content.  Perhaps the image and vector " \
                              "do not overlap or the projections may " \
                              "not be able to be automatically reconciled?")

        return window


    def proj_to_raster(self, projx, projy):
        '''
        Method to convert points in projection space to points in raster space.

        Input can be in a variety of types as long as both the input parameters
        are of the same type.  The method will attempt to return a data type
        as similar as possible to the input type.

        Parameters
        ----------
        projx : int, float, list, tuple, or numpy.ndarray
            Input point in projected space.
        projy : int, float, list, tuple, or numpy.ndarray
            Input point in projected space.

        Returns
        -------
        x : float, list, tuple, or numpy.ndarray
            raster x value calculated from projx, projy, and the object's
            geo_transform
        y : float, list, tuple, or numpy.ndarray
            raster y value calculated from projx, projy, and the object's
            geo_transform
        '''

        # This method can't handle mixed types
        if type(projx) != type(projy):
            raise ValueError('The type of x and y should be the same and '
                             'either integers, float, tuples, lists, or '
                             'numpy arrays.')

        # Determine input type so that the conversion back can be done
        # on return
        intype = 0
        if not hasattr(projx, '__iter__'):
            intype = 1
        elif isinstance(projx, list):
            intype = 2
        elif isinstance(projx, tuple):
            intype = 3
        elif isinstance(projx, np.ndarray):
            intype = 4
        else:
            raise ValueError("The input type was not recognized.")

        # Convert to numpy arrays for the calculation
        projx = np.asarray(projx)
        projy = np.asarray(projy)

        # Get geo_transform from object
        gm = self.meta.geo_transform

        # Transform per inverse of http://www.gdal.org/gdal_datamodel.html
        x = (gm[5] * (projx - gm[0]) - gm[2] * (projy - gm[3])) / \
            (gm[5] * gm[1] + gm[4] * gm[2])
        y = (projy - gm[3] - x * gm[4]) / gm[5]

        # Return to input type
        if intype == 1:
            return float(x), float(y)
        elif intype == 2:
            return list(x), list(y)
        elif intype == 3:
            return tuple(x), tuple(y)
        elif intype == 4:
            return x, y


    def raster_to_proj(self, x, y):
        '''
        Method to convert points in raster space to points in projection space.

        Input can be in a variety of types as long as both the input parameters
        are of the same type.  The method will attempt to return a data type
        as similar as possible to the input type.

        Parameters
        ----------
        x : int, float, list, tuple, or numpy.ndarray
            Input point in raster space.
        y : int, float, list, tuple, or numpy.ndarray
            Input point in raster space.

        Returns
        -------
        projx : float, list, tuple, or numpy.ndarray
            projection x value calculated from x, y, and the object's
            geo_transform
        projy : float, list, tuple, or numpy.ndarray
            projection y value calculated from x, y, and the object's
            geo_transform
        '''

        # This method can't handle mixed types
        if type(x) != type(y):
            raise ValueError('The type of x and y should be the same and '
                             'either integers, float, tuples, lists, or '
                             'numpy arrays.')

        # Determine input type so that the conversion back can be done
        # on return
        intype = 0
        if not hasattr(x, '__iter__'):
            intype = 1
        elif isinstance(x, list):
            intype = 2
        elif isinstance(x, tuple):
            intype = 3
        elif isinstance(x, np.ndarray):
            intype = 4
        else:
            raise ValueError("The input type was not recognized.")

        # Convert to numpy arrays for the calculation
        x = np.asarray(x)
        y = np.asarray(y)

        # Get geo_transform from object
        gm = self.meta.geo_transform

        # Transform per http://www.gdal.org/gdal_datamodel.html
        projx = gm[0] + gm[1] * x + gm[2] * y
        projy = gm[3] + gm[4] * x + gm[5] * y

        # Return to input type
        if intype == 1:
            return float(projx), float(projy)
        elif intype == 2:
            return list(projx), list(projy)
        elif intype == 3:
            return tuple(projx), tuple(projy)
        elif intype == 4:
            return projx, projy


    def get_data(self, component=None,
                       bands=None,
                       window=None,
                       buffer=None,
                       geom=None,
                       mask=False,
                       mask_all_touched=False,
                       virtual=False,
                       return_location=False):
        """Read data from geo-image file.  If component is specified and
        this is a .vrt or .til file, then it will pull only the data from
        the file specified in self.dfile_tiles.  Component is specified base 1.

        There isn't generally a reason to call both component and window,
        but it isn't explicitly disallowed.  If window and component are both
        specified, the resulting data window is relative to the coordinates
        of the specified component.

        If return location=True, the function also returns the upper-left pixel 
        coordinates.

        (TO DO: DETAILED DOCUMENTATION OF INPUT AND OUTPUT! WHAT DO THE ARGUMENTS MEAN?)
        """

        if component is not None:
            if component == 0:
                raise ValueError("Component should be specified as based 1.")

            if component > len(self.files.dfile_tiles):
                raise ValueError("You've requested a component value greater "
                                 "than the number available.")

            y = GeoImage(self.files.dfile_tiles[component-1])
            logger.debug('returning data from:  '+
                  str(self.files.dfile_tiles[component-1]))
            obj = y._fobj
        else:
            obj = self._fobj

        # If bands not requested, set to pull all bands
        if not bands:
            bands = range(1,obj.RasterCount+1)

        # Set window to pull
        if window and geom:
            raise ValueError("The arguments window and geom are mutually " \
                             "exclusive.  They both define an image " \
                             "extent.  Pass either one or the other.")

        if window:
            # Set extent parameters based on window if provided
            if len(window) == 4:
                [xoff, yoff, win_xsize, win_ysize] = window
            else:
                raise ValueError("Window must be length four and will be read" \
                                 "as: xoff, yoff, win_xsize, win_ysize")
        elif geom:
            # Set window size based on a geom object in image space
            # ToDo - Add all_touched option to this and mask function.
            g = self._instantiate_geom(geom)
            extent = g.GetEnvelope()
            window = self._extent_to_window(extent)
            [xoff, yoff, win_xsize, win_ysize] = window
        else:
            # Else use extent of image to set extent params
            xoff = 0
            yoff = 0
            win_xsize = self.meta.shape[1]
            win_ysize = self.meta.shape[2]

        # Add buffer
        if buffer:
            if isinstance(buffer,int):
                buffer = [buffer]

            if len(buffer) == 1:
                xbuff = buffer[0]
                ybuff = buffer[0]
            elif len(buffer) == 2:
                xbuff = buffer[0]
                ybuff = buffer[1]
            else:
                raise ValueError("Buffer must be either length one or two.")

            # Apply the buffer to the readasarray parameters
            xoff = xoff-xbuff
            yoff = yoff-ybuff
            win_xsize = win_xsize+2*xbuff
            win_ysize = win_ysize+2*ybuff

        # Handle out-of-bounds cases
        # (i.e. xoff = 0; buffer = 3; xoff - buffer)
        # initialize buffer vars
        np_xoff_buff = 0
        np_yoff_buff = 0
        np_xlim_buff = 0
        np_ylim_buff = 0

        if xoff < 0:
            np_xoff_buff = xoff
            win_xsize = win_xsize + xoff
            xoff = 0

        if yoff < 0 :
            np_yoff_buff = yoff
            win_ysize = win_ysize + yoff
            yoff = 0

        xpos = xoff+win_xsize
        xlim = self.meta.shape[1]
        ypos = yoff+win_ysize
        ylim = self.meta.shape[2]

        if xpos > xlim:
            np_xlim_buff = xpos-xlim
            win_xsize = win_xsize-np_xlim_buff
        if ypos > self.meta.shape[2]:
            np_ylim_buff = ypos-ylim
            win_ysize = win_ysize-np_ylim_buff

        # # This code will just buffer window requests outside of the
        # # image dimension, so I need to explicitly catch bad requests
        # if np.abs(np_xoff_buff) > xbuff or \
        #    np.abs(np_xlim_buff) > xbuff or \
        #    np.abs(np_yoff_buff) > ybuff or \
        #    np.abs(np_ylim_buff) > ybuff:
        #    raise ValueError("Requested window is outside the image.")

        # Read data
        if virtual is True:
            raise NotImplementedError('keyword argument not implemented yet.')
        elif virtual is False:
            # Read data one band at a time with ReadAsArray
            for i,b in enumerate(bands):
                bobj = obj.GetRasterBand(b)
                # The try/except below is used to initialize the data variable...
                # The first loop trigger the NameError, so the except is used to
                # then initialize "data".
                try:
                    data[i,:,:] = bobj.ReadAsArray(xoff=xoff,
                                                   yoff=yoff,
                                                   win_xsize=win_xsize,
                                                   win_ysize=win_ysize)
                except NameError:
                    zt = len(bands)
                    dt = const.DICT_GDAL_TO_NP[bobj.DataType]
                    data = np.empty([zt, win_ysize, win_xsize], dtype=dt)
                    data[i,:,:] = bobj.ReadAsArray(xoff=xoff,
                                                   yoff=yoff,
                                                   win_xsize=win_xsize,
                                                   win_ysize=win_ysize)
        else:
            raise ValueError("virtual keyword argument should be boolean.")

        # Convert numpy array to masked numpy array if requested.
        if mask and geom:
            # Set image parameters
            xres = self.meta.resolution[0]
            yres = self.meta.resolution[1]
            (xmin, xmax, ymin, ymax) = g.GetEnvelope()
            ul_env = [xmin, ymax]
            ul_raster = self.proj_to_raster(*ul_env)
            ul_corner = [math.floor(ul_raster[0]),math.floor(ul_raster[1])]
            xmin_corner,ymax_corner = self.raster_to_proj(*ul_corner)

            # Create temporary raster to burn
            drv = gdal.GetDriverByName('MEM')
            tds = drv.Create('', win_xsize, win_ysize, 1, gdal.GDT_Byte)
            tds.SetGeoTransform((xmin_corner, xres, 0, ymax_corner, 0, -yres))
            tds.SetProjection(self.meta.projection_string)

            # Create ogr layr from geom
            odrv = ogr.GetDriverByName('Memory')
            ds = odrv.CreateDataSource('')

            ltype = g.GetGeometryType()
            lsrs = osr.SpatialReference(self.meta.projection_string)
            lyr = ds.CreateLayer('burnshp', lsrs, ltype)

            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetGeometryDirectly(g)
            lyr.CreateFeature(feat)

            # Run the burn
            if mask_all_touched:
                err = gdal.RasterizeLayer(tds, [1], lyr, burn_values=[1],
                                                options=['ALL_TOUCHED=TRUE'])
            else:
                err = gdal.RasterizeLayer(tds, [1], lyr, burn_values=[1])

            # build the masked array
            m = tds.ReadAsArray().astype('bool')
            data = np.ma.array(data, mask=np.tile(~m, (data.shape[0], 1, 1)))

        if mask and not geom:
            # This code will mask values outside the image as well as zeros
            # inside the image.
            data = np.ma.array(data,mask=~data.astype('bool'))
            # The line below will only mask values outside the image.
            # data = np.ma.array(data,mask=np.zeros(data.shape).astype('bool'))

        # Pad the output array if needed
        pad_tuples = ((0,0),
                      (np.abs(np_yoff_buff), np.abs(np_ylim_buff)),
                      (np.abs(np_xoff_buff), np.abs(np_xlim_buff)))
        if not mask:
            data = np.pad(data, pad_tuples, 'constant',constant_values=0)
        elif mask:
            mpad = np.pad(data.mask, pad_tuples, 'constant', constant_values=1)
            data = np.pad(data, pad_tuples, 'constant', constant_values=0)
            data = np.ma.array(data,mask=mpad)

        # if "y" is open, close it
        try:
            y = None
        except NameError:
            pass

        if return_location:
            location_dict = {}
            location_dict['upper_left_pixel'] = [xoff, yoff]
            return data, location_dict
        else:
            return data


    def _instantiate_geom(self,g):
        """Attempt to convert the geometry pass in to an ogr Geometry
        object.  Currently implements the base ogr.CreateGeometryFrom*
        methods and will reform fiona geometry dictionaries into a format
        that ogr.CreateGeometryFromJson will correctly handle.
        """

        if isinstance(g,ogr.Geometry):
            # If the input geometry is already an ogr object, create a copy
            # of it.  This is requred because of a subtle issue that causes
            # gdal to crash if the input geom is used elsewhere.  The working
            # theory is that the geometry is freed when going out of a scope
            # while it is needed in the upper level loop.  In this code, the
            # problem comes about between self.iter_vector and self.get_data
            # with mask=True.
            return ogr.CreateGeometryFromJson(str(g.ExportToJson()))

        # Handle straight ogr GML
        try:
            return ogr.CreateGeometryFromGML(g)
        except:
            pass
        # Handle straight ogr Wkb
        try:
            return ogr.CreateGeometryFromWkb(g)
        except:
            pass
        # Handle straight ogr Json
        try:
            return ogr.CreateGeometryFromJson(g)
        except:
            pass
        # Handle straight ogr Wkt
        try:
            return ogr.CreateGeometryFromWkt(g)
        except:
            pass
        # Handle fiona Json geometry format
        try:
            gjson = str(g).replace('(','[').replace(')',']')
            return ogr.CreateGeometryFromJson(gjson)
        except:
            pass

        raise ValueError("A geometry object was not able to be created from " \
                         "the value passed in.")


    def write_img_like_this(self,new_fname,np_array,return_obj=False,
                            gdal_driver_name=None,options=[],
                            vrt_fallback="GTiff"):
        """Write the data passed in the variable "np_array" as a new image
        with image parameters (projection, etc.) pulled from this object.
        The new file is created as the data type of the numpy array
        "np_array".  "np_array" should have the same line/sample size as
        the object data, but the number of bands can vary.  This method uses
        the create_geo_image function in this module.  That function uses
        gdal create and has minimal metadata copy since what should or
        should not be copied is application dependent.
        """

        # If the data array is 2D, add a dimension for a single band
        if np_array.ndim == 2:
            np_array = np_array[np.newaxis, :, :]
        elif np_array.ndim == 3:
            pass
        else:
            raise ValueError("This can't handle Arrays larger than three "
                             "dimensions.")

        if not ((np_array.shape[1] == self.shape[2]) and \
                (np_array.shape[2] == self.shape[1])):
            raise ValueError("Data in is not the same size as that in the "
                             "current image object.  Data in is shape: %s, "
                             "Object shape is: %s" %
                             (np_array.shape, self.shape))

        if gdal_driver_name is None:
            gdal_driver_name = self.meta.driver_name

        ## Write the geo image using my geoio function
        # NDV set to 0 by default in create_geo_image
        new_img_name = create_geo_image(new_file_name = new_fname,
                                        data_np_array = np_array,
                                        gdal_driver_name = gdal_driver_name,
                                        #gdal_driver_name = 'GTiff',
                         gdal_geo_t = self.meta.geo_transform,
                                        gdal_projection = self.meta.projection_string,
                                        data_type = np_array.dtype,
                                        NDV=self.meta.no_data_value,
                                        options=options,
                                        vrt_fallback=vrt_fallback)

        # Output information about the new file
        f = GeoImage(new_img_name)

        logger.debug('')
        logger.debug("######################")
        logger.debug("New image file has been created at:  "+new_img_name)
        logger.debug("Data type is:  "+str(np_array.dtype))
        if self.meta.no_data_value:
            logger.debug("No data value set to:  " +
                         str(self.meta.no_data_value))
        else:
            logger.debug("No data value was not set.")
        logger.debug("######################")

        if return_obj:
            return f


    def write_img_replace_this(self,np_array):
        """Replace the data in the current object image with the data passed
        in the variable "np_array".  This method uses gdal to replace the
        data in place - so all metadata is intact.
        """
        # All dims must be exact
        if not ((np_array.shape[0] == self.shape[0]) and \
                (np_array.shape[1] == self.shape[2]) and \
                (np_array.shape[2] == self.shape[1])):
            raise ValueError("Data in is not the same shape as that in the "
                             "current image object.  Data in is shape: %s, "
                             "Object shape is: %s" %
                             (np_array.shape, self.shape))

        # Check that the incoming array is the same dtype as the image.
        gi_dtype = self.meta.gdal_dtype
        if not (np_array.dtype == const.DICT_GDAL_TO_NP[gi_dtype]):
            raise ValueError("Data types must match exactly to do a "
                             "data replace.")

        # Reload gdal object in update mode
        reload_fname = self.meta_fname
        self._fobj = None
        self._fobj = gdal.Open(reload_fname, gdalconst.GA_Update)

        # Load new data into gdal object file
        nbands = self.shape[0]
        for i in xrange(nbands):
            b = self._fobj.GetRasterBand(i+1).WriteArray(np_array[i,:,:])
            b = None

        # Dereference gdal object and reopen as read only
        self._fobj = None
        self._fobj = gdal.Open(reload_fname, gdalconst.GA_ReadOnly)

    # ToDo Add ability to repoject data during file write.

    def get_stretch_values(self,**kwargs):
        # Call the stretch_values function in this module.
        return get_img_stretch_vals(self._fobj,**kwargs)


def read_geo_file_info(fname_or_fobj):
    """ Get image metadata."""
    # class       : RasterBrick
    # dimensions  : 3191, 921, 2938911, 11  (nrow, ncol, ncell, nlayers)
    # resolution  : 30, 30  (x, y)
    # extent      : 630885, 658515, 2796285, 2892015  (xmin, xmax, ymin, ymax)
    # coord. ref. : +proj=utm +zone=40 +datum=WGS84 +units=m +no_defs +ellps=WGS84 +towgs84=0,0,0
    # data source : /path/to/file/desertEO1H1580422003134110PZ.TIFF
    # names       : desertEO1H1580422003134110PZ.1, desertEO1H1580422003134110PZ.2, desertEO1H1580422003134110PZ.3, desertEO1H1580422003134110PZ.4, desertEO1H1580422003134110PZ.5, desertEO1H1580422003134110PZ.6, desertEO1H1580422003134110PZ.7, desertEO1H1580422003134110PZ.8, desertEO1H1580422003134110PZ.9, desertEO1H1580422003134110PZ.10, desertEO1H1580422003134110PZ.11

    # If the filename passed in is a string, try to open the file, else you
    # can assume that it is a file object already opened and passed in.
    if isinstance(fname_or_fobj,str):
        fobj = gdal.Open(fname_or_fobj, gdalconst.GA_ReadOnly)
    else:
        fobj = fname_or_fobj

    summary = {}
    summary['file_name'] = fobj.GetDescription()
    summary['file_list'] = fobj.GetFileList()
    summary['driver_name'] = fobj.GetDriver().ShortName
    summary['no_data_value'] = fobj.GetRasterBand(1).GetNoDataValue()
    summary['gdal_dtype'] = fobj.GetRasterBand(1).DataType
    summary['gdal_dtype_name'] = gdal.GetDataTypeName(summary['gdal_dtype'])

    ### Get Image basics dimensions
    x = fobj.RasterXSize
    y = fobj.RasterYSize
    summary['pixels'] = x*y
    nbands = fobj.RasterCount
    summary['shape'] = (nbands, x, y)

    ### Get image resolution and extents
    # Pulled from GDALInfoReportCorner() at:
    # http://www.gdal.org/gdalinfo_8c.html
    gt = fobj.GetGeoTransform()
    summary['geo_transform'] = gt
    xstart = gt[0]
    xend = gt[0] + gt[1]*x + gt[2]*y
    ystart = gt[3]
    yend = gt[3] + gt[4]*x + gt[5]*y

    # Pixel Resolutions
    xres = abs(gt[1])
    yres = abs(gt[5])

    # Add to summary
    summary['resolution'] = (xres, yres)
    summary['extent'] = (xstart, xend, ystart, yend)

    ### Get image projection and datum
    summary['projection_string'] = fobj.GetProjection()
    # Get pretty printable projection_string
    sr = osr.SpatialReference(fobj.GetProjectionRef())
    summary['pprint_proj_string'] = sr.ExportToPrettyWkt()
    summary['authority'] = sr.GetAttrValue("AUTHORITY",0)+ ':' + \
                           sr.GetAttrValue("AUTHORITY",1)

    return summary


# Function to write a new file.
def create_geo_image(new_file_name, data_np_array, gdal_driver_name,
                     gdal_geo_t, gdal_projection, data_type, NDV=0,
                     options=[],vrt_fallback="GTiff"):
    """ Create a new image file with all the parameters pass in.  This
    function is implemented in the GeoImage Class.  It will autopopulate the
    parameters so is a bit easier to use from there.
    """

    if gdal_driver_name == "VRT":
        # Change driver name to something that will write to disk.  This
        # defaults to GTiff but is configurable.
        logger.debug("You have requested a VRT.  Creating a single new real "
                     "file using %s format.  This format can be ovridden "
                     "in the call using 'GTiff', 'ENVI', etc.  Eventually, "
                     "this could be replaced with a file-for-file VRT "
                     "rewrite, but that does not always make sense in VRTs "
                     "since there can be pixels in the original image "
                     "(overlaps) that aren't in the "
                     "new data array.",vrt_fallback)
        gdal_driver_name = vrt_fallback

        # Strip ".vrt" extension if it exists and rename according to the
        # driver that has been substituted.
        # --- Only supports ENVI and GTiff now, I can't figure out how to do
        #  this dynamically in gdal.  (as of 141218)
        if tt.files.filter(new_file_name, '*.VRT', case_sensitive=False):
            if gdal_driver_name == "GTiff":
                new_file_name = os.path.splitext(new_file_name)[0] + ".TIF"
            elif gdal_driver_name == "ENVI":
                new_file_name = os.path.splitext(new_file_name)[0]
            else:
                new_file_name = os.path.splitext(new_file_name)[0]

    ## Convert data types to the appropriate value.  The gdal create
    ## routine can pass either the gdal data type int or the alias (such as
    ## gdal.GDT_Float32).
    # If the data type passed is of type numpy, convert it to GdalDataType
    if data_type in const.DICT_NP_TO_GDAL:
        data_type = const.DICT_NP_TO_GDAL[data_type]
    # If gdal dtype class then pass
    elif data_type in const.DICT_GDAL_TO_NP:
        # Maybe convert to the integer form?
        pass
    # if gdal dtype name, then convert to data type integer alias
    elif isinstance(data_type,str):
        data_type = gdal.GetDataTypeByName(data_type)

    # If the data array is 2D, add a dimension for a single band
    if data_np_array.ndim == 2:
        data_np_array = data_np_array[np.newaxis, :, :]
    elif data_np_array.ndim == 3:
        pass
    else:
        raise ValueError("This can't handle Arrays larger than three "
                         "dimensions.")
    (n_bands,y_size,x_size) = data_np_array.shape

    # Create driver and data set object
    driver = gdal.GetDriverByName(gdal_driver_name)
    # Check that the driver supports the data_np_array.dtype
    dstr = gdal.GetDataTypeName(const.DICT_NP_TO_GDAL[data_np_array.dtype])
    dlist = driver.GetMetadata()['DMD_CREATIONDATATYPES'].split()
    if not dstr in dlist:
        raise ValueError("The data type of the provided numpy array is not "
                         "supported by the requested file format.")
    if options:
        if not isinstance(options,list):
            raise ValueErrror("The options list is malformed.  It should be a "
                              "list of strings")
    dst_ds = driver.Create(new_file_name, x_size, y_size, n_bands,
                           data_type, options)

    # Set projection and geotransform
    dst_ds.SetGeoTransform(gdal_geo_t)
    dst_ds.SetProjection(gdal_projection)

    ### Write the new data
    # Set nans to the original No Data Value
    if NDV is not None:
        data_np_array[np.isnan(data_np_array)] = NDV
        data_np_array[np.isinf(data_np_array)] = NDV
        ### Other???

    # Write the bands
    for b in range(n_bands):
        dst_ds.GetRasterBand(b+1).WriteArray( data_np_array[b,:,:] )
        if NDV is not None:
            dst_ds.GetRasterBand(b+1).SetNoDataValue( NDV )

    # Once we're done, close properly the dataset
    dst_ds = None

    # Return the new file name in case it was changed due to vrt fallback.
    return new_file_name


def get_img_stretch_vals(imgfname_or_gdalobj,stretch=[0.02,0.98],approx_ok=True):
    """Read image, mask very small values, and return max and min

    imgfname_or_gdalobj: Either a file name string of a gdal object
    stretch:               stretch range to return in a list
    approx_ok:           Is it ok to approximate the max/min values (bool)
    hist_nbins:          How many bins the histogram is made of to find the
                         appropriate cut value (more bins is more accurate,
                         but slower).
    """

    # ToDo Add tests for get_img_strech_vals
    ### Cases to consider:
    # Lots of fill 0 values with much larger data
    # Lots of fill 0 values with data near zero
    # Large integers
    # Floating point 0-1
    # Floating point larger than 1
    # Negative values to postivie values (a la NDVI)
    #... basically this needs to respect the No Data Value and let the math fly

    if isinstance(imgfname_or_gdalobj,str):
        f = gdal.Open(imgfname_or_gdalobj)
    elif isinstance(imgfname_or_gdalobj,gdal.Dataset):
        f = imgfname_or_gdalobj
    else:
        raise ValueError("The variable passed in as imgfname_or_gdalobj is "
                         "neither an image filename string or a gdal object.")

    # Setup for query of each band
    outofrange = 0  # value zero means no, don't include out of range
    hist_nbins = 1000
    if approx_ok:
        approx = 1
    else:
        approx = 0

    for i in range(1,f.RasterCount+1):
        # ToDo Add respect for NoDataValue

        # Get gdal band object
        b = f.GetRasterBand(i)

        # Get band max/min
        tmp = b.ComputeRasterMinMax(approx)
        imgmax = tmp[1]
        imgmin = tmp[0]

        # Use max/min to get histogram
        hist = b.GetHistogram(imgmin,imgmax,hist_nbins,outofrange,approx_ok)
        hist = np.asarray(hist)
        bins = np.linspace(imgmin, imgmax, len(hist))

        # Get stretch values
        cdf = hist.cumsum()
        top = cdf.max() * stretch[1]
        bottom = cdf.max() * stretch[0]

        topcut = bins[np.where(cdf > top)[0][0]]
        bottomcut = bins[np.where(cdf < bottom)[0][-1]]

    return (bottomcut,topcut)

