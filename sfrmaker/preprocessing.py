import os
import shutil
import yaml
import textwrap
import numpy as np
import pandas as pd
import fiona
import rasterio
from shapely.geometry import shape, MultiLineString, box
from rasterstats import zonal_stats
from sfrmaker.gis import shp2df, df2shp, project, intersect_rtree, CRS, get_bbox, read_polygon_feature
from sfrmaker.elevations import smooth_elevations
from sfrmaker.logger import Logger
from sfrmaker.nhdplus_utils import get_nhdplus_v2_filepaths, get_prj_file
from sfrmaker.routing import find_path, make_graph
from sfrmaker.units import convert_length_units
from sfrmaker.utils import width_from_arbolate_sum


def cull_flowlines(NHDPlus_paths,
                   active_area=None,
                   asum_thresh=None,
                   intermittent_streams_asum_thresh=None,
                   cull_invalid=True,
                   cull_isolated=True,
                   outfolder='clipped_flowlines', logger=None):
    """Cull NHDPlus data to an area defined by an ``active_area`` polygon and
    to flowlines with Arbolate sums greater than specified thresholds. Also remove
    lines that are isolated from the stream network or are missing attribute information.

    Parameters
    ----------
    NHDPlus_paths : list of strings
        Paths to the root folder level NHDPlus Drainage Basins, as they were downloaded
        from the NHDPlus website (e.g. NHDPlus04, NHDPlus07, etc.).
    active_area : str, shapely polygon or tuple, optional
        A polygon shapefile, or shapely polygon or bounding box tuple
        (left, bottom, top, right) in the NAD83 GCS (EPSG:4269). The active area
        is converted to a bounding box, which is then used to filter the flowlines
        that are read in. If none, no filtering is performed, and the whole
        area encompased by the input NHDPlus data will be retained.
        By default None.
    asum_thresh : numeric, optional
        Minimum arbolate sum value (total length of upstream drainage) to
        retain. Any flowlines with arbolate sums less than this value will be dropped.
        By default None.
    intermittent_streams_asum_thresh : numeric, optional
        Minimum arbolate sum value (total length of upstream drainage) to
        retain for flowlines coded as intermittent (FCODE == 46003).
        Any intermittent flowlines with arbolate sums less than this value will be dropped.
        By default None.
    cull_invalid : bool, optional
        Option to cull flowlines that have incomplete attribute information
        (lacking entries in the PlusFlowVAA, PlusFlow or Elevslope tables), by default False.
    cull_isolated : bool, optional
        Culling intermittent streams with intermittent_streams_asum_thresh may result
        in some isolated flowlines that no longer have downstream connections, or
        such isolated flowlines may be present in the raw NHDPlus data.
        SFRmaker identifies isolated flowslines by looking at up to 10 downstream connections
        to lines that are not stream network. Option to drop
        these lines. By default, False.
    outfolder : str, optional
        Location for writing output, by default 'clipped_flowlines'
    logger : sfrmaker.logger instance, optional
        Pass an existing sfrmaker.logger instance to logger the preprocessing operations,
        by default None
    """
    if logger is None:
        logger = Logger()
    logger.log('Culling NHDPlus dataset')
    logger.log('Reading raw NHDPlus files')

    if not os.path.exists(outfolder):
        os.makedirs(outfolder)
        logger.statement('created {}'.format(outfolder))

    if asum_thresh is not None:
        version = '_gt{:.0f}km'.format(asum_thresh)
    else:
        version = ''
    flowlines_files, pfvaa_files, pf_files, elevslope_files = \
        get_nhdplus_v2_filepaths(NHDPlus_paths)

    # logger the date modified timestamps for the NHDPlus files
    for f in flowlines_files + pfvaa_files + pf_files + elevslope_files:
        logger.log_file_and_date_modified(f)

    # get crs information from flowline projection file
    prjfile = get_prj_file(NHDPlus_paths)
    nhdcrs = CRS(prjfile=prjfile)

    if isinstance(active_area, tuple):
        extent_poly_nhd_crs = box(*active_area)
        filter = active_area
    elif active_area is not None:
        extent_poly_nhd_crs = read_polygon_feature(active_area, nhdcrs)
        # ensure that filter bbox is in same crs as flowlines
        # get filters from shapefiles, shapley Polygons or GeoJSON polygons
        filter = get_bbox(active_area, dest_crs=nhdcrs)
    else:
        filter = None

    # read NHDPlus files into pandas dataframes
    fl = shp2df(flowlines_files, filter=filter)
    fl_all = fl.copy()

    pfvaa = shp2df(pfvaa_files)
    pf = shp2df(pf_files)
    elevslope = shp2df(elevslope_files)

    logger.log('Reading raw NHDPlus files')

    # index dataframes by common-identifier numbers
    fl.index = fl.COMID.astype(int)
    pfvaa.index = pfvaa.ComID.astype(int)
    pf.index = pf.FROMCOMID.astype(int)
    elevslope.index = elevslope.COMID.astype(int)

    if cull_invalid:
        logger.statement('Dropping flowlines without attribute information')
        # only retain comids that have information in all of the tables
        original_comids = set(fl.index)
        valid_comids = set(fl.index).intersection(pfvaa.index).\
            intersection(pf.index).intersection(elevslope.index)
        # order the same as flowlines
        valid_comids = [c for c in fl.index if c in valid_comids]
        fl = fl.loc[valid_comids]
        pfvaa = pfvaa.loc[valid_comids]
        pf = pf.loc[valid_comids]
        elevslope = elevslope.loc[valid_comids]
        dropped = original_comids.difference(valid_comids)
        if any(dropped):
            logger.statement('Dropping {} of {} lines'.format(len(dropped),
                                                              len(original_comids)),
                             log_time=False)
    else:
        pfvaa = pfvaa.loc[[True if c in fl.index else False for c in pfvaa.index]]
        pf = pf.loc[[True if c in fl.index else False for c in pf.index]]
        elevslope = elevslope.loc[[True if c in fl.index else False for c in elevslope.index]]
    fl['nhd_asum'] = pfvaa.ArbolateSu

    # cull by arbolate sum first
    if asum_thresh is not None:
        logger.statement('Dropping Flowlines with arbolate sum less than {}km'.format(asum_thresh))
        fl = fl.loc[fl.nhd_asum >= asum_thresh]

    # then cull intermittent streams
    if intermittent_streams_asum_thresh is not None:
        logger.statement('Dropping intermittent streams with arbolate sum less than {}km'.format(intermittent_streams_asum_thresh))
        drop_intermittent = (fl.nhd_asum < intermittent_streams_asum_thresh) & (fl.FCODE == 46003)
        fl = fl.loc[~drop_intermittent]

    if cull_isolated:
        # drop any remaining stream segments that are isolated
        # (segments marked as perennial that routed to segment(s) marked as intermittent)
        # looking at Google Satellite, many intermittent segments appear to no longer exist
        # (occur in middle of fields, etc.)
        logger.log('Removing isolated flowlines that are no longer in the network')
        # quick and dirty routing graph
        # technically not correct, because some flowlines have more than one distrib.
        # the tocomid chosen will be the last one element-wise in the plusflow table
        # this should be fine because there weren't many isolated COMIDs in the MAP area
        comids = set(fl.COMID)
        tocomid = [c if c in pf.FROMCOMID else 0 for c in pf.TOCOMID]
        graph = dict(zip(pf.FROMCOMID, tocomid))
        fl['tocomid'] = [graph[c] for c in fl.index]
        geoms = dict(zip(fl_all.COMID, fl_all.geometry))
        drop_comids = {0}
        for i, c in enumerate(fl.COMID):
            # skip comids already in drop list
            if c in drop_comids:
                continue
            path = find_path(graph, c, limit=10)
            assert path is not None
            for j, dnid in enumerate(path):
                # if the downstream flowline is not in the SFR network
                if dnid != 0 and dnid not in comids:
                    # but is within the model domain and a stream
                    # it was dropped
                    g = geoms.get(dnid, None)
                    dnid_fcode = pfvaa.loc[dnid, 'Fcode']
                    if g is not None and g.within(extent_poly_nhd_crs) and dnid_fcode not in [56600 # coastline
                                                                                           ]:
                        # drop current comid and all upstream comids
                        drop_comids.update(set(path[:j+1]))
                        break

        fl = fl.loc[~fl.COMID.isin(drop_comids)]

        logger.log('Removing isolated flowlines that are no longer in the network')
        logger.statement('Removed {} of {} flowlines'.format(len(drop_comids) - 1,
                                                             len(comids)),
                         log_time=False)

    # write output files; record timestamps in logger
    logger.statement('writing output')
    results = {'flowlines_file': '{}/flowlines{}.shp'.format(outfolder, version),
               'pfvaa_file': '{}/PlusFlowlineVAA{}.dbf'.format(outfolder, version),
               'pf_file': '{}/PlusFlow{}.dbf'.format(outfolder, version),
               'elevslope_file': '{}/elevslope{}.dbf'.format(outfolder, version)
               }
    df2shp(fl, results['flowlines_file'], epsg=4269)
    df2shp(pfvaa, results['pfvaa_file'])
    df2shp(pf, results['pf_file'])
    df2shp(elevslope, results['elevslope_file'])
    logger.log('Culling NHDPlus dataset')
    return results


def preprocess_nhdplus(flowlines_file, pfvaa_file,
                       pf_file, elevslope_file,
                       demfile,
                       dem_length_units='meters',
                       narwidth_shapefile=None,
                       waterbody_shapefiles=None,  # for sampling NARWidth
                       buffersize_meters=50,
                       asum_thresh=None,
                       known_connections=None,
                       width_from_asum_a_param=0.1193,
                       width_from_asum_b_param=0.5032,
                       minimum_width=1.,
                       output_length_units='meters',
                       logger=None, outfolder='output/',
                       project_epsg=None,
                       ):
    """Preprocess NHDPlus data to a single DataFrame of flowlines
    that each route to no more than one flowline, with width, elevation
    and recomputed arbolate sum attributes. A key part of the preprocessing is handling divergences in the stream network, as described in more detail in the ``Notes`` section. In picking routing at divergences, elevations are sampled from the ``demfile`` and included in the output DataFrame. Optionally (via the ``narwidth_shapefile`` arguement), remote sensing-based width estimates from the NARWidth Database (Allen and Pavelsky, 2015) can be included.

    Parameters
    ----------
    flowlines_file : str
        Path to NHDPlus NHDFlowline shapefile. May or maybe not have been
        preprocessed by :func:`~sfrmaker.preprocessing.cull_flowlines`. The flowlines
        must be in a valid projected coorinate reference system (CRS; i.e., with units of meters),
        or a valid projected CRS must be specified with ``project_epsg``.
    pfvaa_file : str
        Path to NHDPlus PlusFlowlineVAA database (.dbf file). May or maybe not have been
        preprocessed by :func:`~sfrmaker.preprocessing.cull_flowlines`. ``ArbolateSu``
        values within this file are assumed to be in km.
    pf_file : str
        Path to NHDPlus PlusFlow database (.dbf file). May or maybe not have been
        preprocessed by :func:`~sfrmaker.preprocessing.cull_flowlines`
    elevslope_file : str
        Path to NHDPlus elevslope database (.dbf file). May or maybe not have been
        preprocessed by :func:`~sfrmaker.preprocessing.cull_flowlines`
    demfile : str
        Path to DEM raster for project area.
    dem_length_units : str, any length unit; e.g. {'m', 'meters', 'ft', etc.}
        Length units of elevations in ``demfile``. By default, 'meters'.
    narwidth_shapefile : str, optional
        Path to shapefile from the NARWidth database (Allen and Pavelsky, 2015).
    waterbody_shapefiles : str or list of strings, optional
        Path(s) to NHDPlus NHDWaterbody shapefile(s). Only required if a
        ``narwidth_shapefile`` is specified.
    buffersize_meters : float
        Buffer distance in meters around flowlines to include when sampling DEM.
        By default, 50.
    asum_thresh : float
        Arbolate sum threshold for culling minor distributaries
        (that are not the main channel) below divergences.
        In NHDPlus, minor distributaries have the same arbolate sum as the main channel.
        After selecting the main channel, SFRmaker recomputes arbolate sum values
        for the minor distributaries, starting with 0 at the divergence. Lines with
        ending asums less than ``asum_thresh`` will then be culled.
    known_connections : dict, optional
        Dictionary of specified flowline connections {COMID: tocomid},
        which will override the routing selection at distributaries.
        By default None.
    width_from_asum_a_param : float, optional
        :math:`a` parameter used for estimating channel width from arbolate sum.
        Only needed if input flowlines are lacking width information.
        See :func:`~sfrmaker.utils.width_from_arbolate`. By default, 0.1193.
    width_from_asum_b_param : float, optional
        :math:`b` parameter used for estimating channel width from arbolate sum.
        Only needed if input flowlines are lacking width information.
        See :func:`~sfrmaker.utils.width_from_arbolate`. By default, 0.5032.
    minimum_width : float, optional
        Minimum reach width to specify (in model units), if computing widths from
        arbolate sum values. (default = 1)
    output_length_units : str, any length unit; e.g. {'m', 'meters', 'ft', etc.}
        Units for width and elevation attribute values included with the output flowlines.
        Output arbolate sum values are specified in kilometers.
    outfolder : str, optional
        Location for writing output, by default 'clipped_flowlines'
    logger : sfrmaker.logger instance, optional
        Pass an existing sfrmaker.logger instance to logger the preprocessing operations,
        by default None
    project_epsg : int
        EPSG code for the output CRS (e.g. 5070 for NAD83 Albers).
        Required if the flowlines are not in a valid Projected CRS.

    Returns
    -------
    flowlines : DataFrame
        DataFrame with preprocessed flowlines. Width and elevation values are specified
        in the ``output_length_units``. Output arbolate sum values are specified
        in kilometers. See NHDPlus documentation for description of fields not
        included here. Columns:

        =========================== ================  ==============================================
        **COMID**                   int64             NHDPlus Common Identifiers
        **tocomid**                 int64             Downstream routing connections (COMIDs)
        **nhd_asum**                float             Arbolate sum from NHDPlus, in km
        **min**                     float             minimum elevation sampled within each buffer
        **mean**                    float             mean elevation sampled within each buffer
        **pct10,20,80**             float             elevation percentiles sampled within each buffer
        **Divergence**              int               NHDPlus Divergence classification
        **main_chan**               bool              Flag indicating whether SFRmaker identified line as main channel
        **elevup**                  float             Smoothed elevation at line start (sampled from DEM)
        **elevdn**                  float             Smoothed elevation at line end (sampled from DEM)
        **elevupsmo**               float             Smoothed elevation at line start (sampled from DEM)
        **elevdnsmo**               float             Smoothed elevation at line end (sampled from DEM)
        **asum_calc**               float             Recomputed arbolate sum for line (after removing distributaries)
        **asum_diff**               float             Difference between recomputed and NHDPlus asums
        **width1asum**              float             asum-based estimate for width at line start
        **width2asum**              float             asum-based estimate for width at line end
        **narwd_n**                 int               number of elevations for line sampled from NARWidth
        **narwd_mean**              float             mean elevation sampled from NARWidth
        **narwd_std**               float             standard deviation in elevations sampled from NARWidth
        **narwd_min**               float             minimum elevation sampled from NARWidth
        **narwd_max**               float             maximum elevation sampled from NARWidth
        **is_wb**                   bool              Flag for whether the line coincides with a Waterbody
        **width1**                  float             estimated width at line start (from NARWidth or asum)
        **width2**                  float             estimated width at line end (from NARWidth or asum)
        **geometry**                obj               Shapely LineString for each line
        **buffpoly**                obj               Shapely Polygon (buffer) around each LineString
        =========================== ================  ==============================================

    Raises
    ------
    ValueError
        [description]
    IOError
        [description]
        
    Notes
    -----
    A key part of the preprocessing is handling divergences in the stream network, where flow is routed to two or more distributaries. Distributaries are common in the Mississippi Alluvial Plain (MAP) region, for example, but in reality, most of these features are either non-existent or only carry flow intermittently during high-water events. While distributaries in NHDPlus are classified as “main” and “minor” paths at divergences, inspection against the satellite imagery and the most recent, `lidar-based DEMs for the MAP area <https://viewer.nationalmap.gov/basic/>`_ suggested that the NHDPlus classifications are often inaccurate. 

    The following steps are taken to identify the main channel at each divergence:
    
    •	A 50-meter buffer polygon is drawn around each flowline feature. A flat end-cap is used, so that only areas perpendicular to the flowlines are included in each buffer.
    •	Zonal statistics for the lidar-based DEM elevations within each buffer polygon are computed using the `rasterstats python package <https://pythonhosted.org/rasterstats/>`_. The tenth percentile elevation is selected as a metric for discriminating between the main channel and minor distributaries. Lower elevation percentiles would be more likely to represent areas of overlap between the buffers for the main channel and minor distributaries (resulting in minor distributary elevations that are similar to the main channel), while higher elevation percentiles might miss the lowest parts of the main channel or even represent parts of the channel banks instead.
    •	At each divergence, the distributary with the lowest tenth percentile elevation is assumed to be the main channel. 
    
    In the MAP region, comparison of the sampled DEM elevations with the NHDPlus elevation attribute data revealed a high bias in many of the attribute elevations, especially in the vicinity of diversions. This may be a result of the upstream smoothing process described by McKay and others (2012, p 123) when it encounters distributaries of unequal elevations such as the example shown in Figure 5. To remedy this issue, the 10th percentile elevations obtained from the buffer zonal statistics were assigned to each flowline, and then smoothed in the downstream direction to ensure that no flowlines had negative (uphill) slopes. 
    
    Finally, routing connections to minor distributaries are removed, and arbolate sums recomputed for the entire stream network, with arbolate sums at minor distributaries starting at zero. In this way, the minor distributaries are treated like headwater streams in that they will only receive flow if the water table is greater than their assigned elevation, otherwise they are simulated as dry and are not part of the groundwater model solution. Similar to :func:`~sfrmaker.preprocessing.cull_flowlines`, the first ``asum_thresh`` km of minor distributaries are trimmed from the stream network.

    If a shapefile is specified for the ``narwidth_shapefile`` argument, the :func:`~sfrmaker.preprocessing.sample_narwidth` function is called.

    """    
    # check that all the input files exist
    files_list = [flowlines_file,
                  pfvaa_file,
                  pf_file,
                  elevslope_file,
                  demfile]
    if narwidth_shapefile is not None:
        if waterbody_shapefiles is None:
            raise ValueError("NARWidth option ")
        else:
            if isinstance(waterbody_shapefiles, str):
                waterbody_shapefiles = [waterbody_shapefiles]
            files_list += waterbody_shapefiles
    for f in files_list:
        assert os.path.exists(f), "missing {}".format(f)
    if known_connections is None:
        known_connections = {}
    if logger is None:
        logger = Logger()

    logger.log('Preprocessing Flowlines')

    # read NHDPlus files into pandas dataframes
    for f in [flowlines_file, pfvaa_file, pf_file, elevslope_file]:
        logger.log_file_and_date_modified(f)

    # get the flowline CRS, if geographic,
    # verify that project_crs is specified
    flowline_crs = None
    project_crs = None
    prjfile = os.path.splitext(flowlines_file)[0] + '.prj'
    if os.path.exists(prjfile):
        flowline_crs = CRS(prjfile=prjfile)
    else:
        msg = ("{} not found; flowlines must have a valid projection file."
               .format(prjfile))
        logger.lraise(msg)
    if project_epsg is not None:
        project_crs = CRS(epsg=project_epsg)
    if flowline_crs.pyproj_crs.is_geographic:
        if project_crs is None or project_crs.pyproj_crs.is_geographic:
            msg = ("project_epsg for a valid Projected CRS (i.e. in units of meters)\n"
                   " must be specified if flowlines are in a Geographic CRS\n"
                   "specified project_epsg: {}".format(project_epsg))
            logger.lraise(msg)

    # get bounds of flowlines
    with fiona.open(flowlines_file) as src:
        flowline_bounds = src.bounds

    fl = shp2df(flowlines_file) # flowlines clipped to model area
    pfvaa = shp2df(pfvaa_file)
    pf = shp2df(pf_file)
    elevslope = shp2df(elevslope_file)

    # index dataframes by common-identifier numbers
    pfvaa.index = pfvaa.ComID
    pf.index = pf.FROMCOMID
    elevslope.index = elevslope.COMID
    fl.index = fl.COMID

    # subset attribute tables to clipped flowlines
    pfvaa = pfvaa.loc[fl.index]
    pf = pf.loc[fl.index]
    elevslope = elevslope.loc[fl.index]

    # reproject the flowlines if they are not in project_crs
    if project_crs is not None and flowline_crs is not None and project_crs != flowline_crs:
        fl['geometry'] = project(fl.geometry, flowline_crs.proj_str, project_crs.proj_str)

    # draw buffers
    flbuffers = [g.buffer(buffersize_meters, cap_style=2)  # 2 (flat cap) very important!
                 for g in fl.geometry]

    # Create buffer around flowlines with flat cap, so that ends are flush with ends of lines
    # compute zonal statistics on buffer
    logger.log('Creating buffers and running zonal statistics')
    logger.log_package_version('rasterstats')
    logger.statement('buffersize: {} m'.format(buffersize_meters), log_time=False)
    logger.log_file_and_date_modified(demfile, prefix='DEM file: ')

    # if DEM has different crs, project buffer polygons to DEM crs
    with rasterio.open(demfile) as src:
        meta = src.meta
        dem_crs = CRS(meta['crs'])
    flbuffers_pr = flbuffers
    if flowline_crs is not None and dem_crs != flowline_crs:
        flbuffers_pr = project(flbuffers, project_crs.proj_str, dem_crs.proj_str)

    # run zonal statistics on buffers
    # this step takes at least ~ 20 min for the full 1-mi MERAS model
    results = zonal_stats(flbuffers_pr,
                          demfile,
                          stats=['min', 'mean', 'percentile_10', 'percentile_20', 'percentile_80'])
    df = pd.DataFrame(results)
    dem_units_to_output_units = convert_length_units(dem_length_units, output_length_units)
    fl['mean'] = df['mean'].values * dem_units_to_output_units
    fl['min'] = df['min'].values * dem_units_to_output_units
    fl['pct10'] = df.percentile_10.values * dem_units_to_output_units
    fl['pct20'] = df.percentile_20.values * dem_units_to_output_units
    fl['pct80'] = df.percentile_80.values * dem_units_to_output_units
    fl['buffpoly'] = flbuffers
    logger.log('Creating buffers and running zonal statistics')

    # write a shapefile of the flowline buffers for GIS visualization
    logger.statement('Writing shapefile of buffers used to determine distributary routing...')
    flccb = fl.copy()
    flccb['geometry'] = flccb.buffpoly
    df2shp(flccb.drop('buffpoly', axis=1),
           os.path.join(outfolder, 'flowlines_gt{}km_buffers.shp'.format(asum_thresh)),
           index=False, epsg=project_epsg)

    # cull COMIDS with invalid elevations
    minelev = -10
    logger.statement('Culling COMIDs with smoothed elevations < {} cm'.format(minelev))
    badstrtop = (elevslope.MAXELEVSMO < minelev) | (elevslope.MINELEVSMO < minelev)
    badstrtop_comids = elevslope.loc[badstrtop].COMID.values
    badstrtop = [True if c in badstrtop_comids else False for c in fl.COMID]
    flcc = fl.loc[~np.array(badstrtop)].copy()

    # add some attributes from pfvaa file
    flcc['Divergence'] = pfvaa.loc[flcc.index, 'Divergence']
    flcc['LevelPathI'] = pfvaa.loc[flcc.index, 'LevelPathI']
    flcc['nhd_asum'] = pfvaa.loc[flcc.index, 'ArbolateSu']

    # dictionary with routing info by COMID
    graph = make_graph(pf.FROMCOMID.values, pf.TOCOMID.values)
    in_model = fl.COMID.tolist()
    graph = {k: v for k, v in graph.items() if k in in_model}

    # use the 10th percentile from zonal_statistics for setting end elevation of each flowline
    # (hopefully distinguishes flowlines that run along channels vs.
    # those perpendicular to channels that route across upland areas)
    elevcol = 'pct10'

    # use zonal statistics elevation to determine routing at divergences
    # (many of these do not appear to be coded correctly in NHDPlus)
    # route to the segment with the lowest 20th percentile elevation
    logger.log('Determining routing at divergences using sampled elevations')
    txt = 'Primary distributary determined from lowest {}th percentile '.format(elevcol[-2:]) +\
          'elevation value among distributaries at the confluence.\n'

    # ensure these connections between comids
    # fromcomid: tocomid
    txt += 'Pre-determined routing at divergences (known_connections):\n'
    for k, v in known_connections.items():
        txt += '{} --> {}\n'.format(k, v)

    logger.statement(txt)
    elevs = dict(zip(flcc.COMID, flcc[elevcol]))
    tocomids = {}
    diversionminorcomids = set()
    for k, v in graph.items():
        # comid routes to only one comid
        if len(v) == 1:
            tocomids[k] = v.pop()
        # comid is an outlet
        elif len(v) == 0:
            tocomids[k] = 0
        elif k in known_connections.keys():
            # primary dist.
            tocomids[k] = known_connections[k]
            # update minorcomids
            diversionminorcomids.update(v.difference({tocomids[k]}))
        # comid routes to multiple comids (diversion)
        else:
            tocomids_c = list(v)
            dnelevs = [elevs.get(toid, 99999) for toid in tocomids_c]
            # primary distributary
            tocomids[k] = np.array(tocomids_c)[np.argmin(dnelevs)]
            # secondary distributaries
            diversionminorcomids.update(v.difference({tocomids[k]}))

    # drop comids not in the model
    diversionminorcomids = diversionminorcomids.intersection(flcc.index)

    # label secondary distributaries
    flcc['main_chan'] = True
    flcc.loc[diversionminorcomids, 'main_chan'] = False

    # verify that all comids only route to one other comid
    assert np.all([np.isscalar(v) for v in tocomids.values()])

    # update the routing graphs
    # set tocomids to zero if there's no flowline
    graph = {k: v if v in flcc.index else 0 for k, v in tocomids.items()}
    graph_r = make_graph(list(graph.values()), list(graph.keys()))
    flcc['tocomid'] = [graph.get(c, 0) for c in flcc.index]
    logger.log('Determining routing at divergences using sampled elevations')

    # Update comid start elevations using new routing
    logger.log('Updating elevations')
    elevup = {}
    cm_to_output_units = convert_length_units('cm', output_length_units)
    # dictionary of NHDPlus minimum elevations converted to output units
    elevslope_dict = dict(zip(elevslope.COMID, elevslope.MINELEVSMO * cm_to_output_units))
    # screen for comids outside model
    valid_comids = {k for k, v in elevs.items() if minelev < v < 1e5}
    for tocomid, fromcomids in graph_r.items():
        # if len(fromcomids) > 0:
        fromcomids = fromcomids.intersection(valid_comids)
        if len(fromcomids) > 0:
            elevup[tocomid] = np.min([elevs[c] for c in fromcomids])
        elif tocomid in valid_comids:
            elevup[tocomid] = elevs[tocomid]

    flcc['elevup'] = [elevup.get(c) for c in flcc.index]
    flcc['elevdn'] = [elevs[c] if -10 < elevs[c] < 1e5 else elevslope_dict[c] for c in flcc.index]
    noelevup = np.isnan(flcc.elevup)
    flcc.loc[noelevup, 'elevup'] = flcc.loc[noelevup, 'elevdn']

    logger.log('Updating elevations')

    # smooth segment end elevations so that they never rise downstream
    elevminsmo, elevmaxsmo = smooth_elevations(flcc.index.values, flcc.tocomid.values,
                                               flcc.elevdn.values, flcc.elevup.values)
    flcc['elevupsmo'] = [elevmaxsmo[c] for c in flcc.index]
    flcc['elevdnsmo'] = [elevminsmo[c] for c in flcc.index]

    # verify that end elevations less than start elevations
    assert np.all(flcc.elevdnsmo <= flcc.elevupsmo)
    # verify that elevations don't rise at segment connections
    elevupsmo = dict(zip(flcc.index, flcc.elevupsmo))
    nextup = np.array([elevupsmo.get(graph.get(c, -10), -10) for c in flcc.index])
    assert np.all(nextup <= flcc.elevdnsmo.values)

    # subtract secondary distributaries
    nhdplus_asums = dict(zip(pfvaa.index, pfvaa.ArbolateSu))
    fl_lengths = fl.LENGTHKM.to_dict()

    logger.log('Recomputing arbolate sums from minor distributaries')

    def recompute_asums_downstream(diversionminorcomids):
        """Reset arbolate sums for minor distributaries and
        downstream segments in their path."""
        asums = {}
        for c in diversionminorcomids:
            path = find_path(graph, c)
            asum_c = 0
            for cp in path:
                tribs = graph_r[cp]
                if cp == 0 or len(tribs) > 1:
                    break
                asum_c += fl_lengths[cp]
                asums[cp] = asum_c
        return asums

    # recompute the arbolate sums so that minor distributaries start at 0
    asum_calc = recompute_asums_downstream(diversionminorcomids)
    flcc['asum_calc'] = [asum_calc.get(c, nhdplus_asums[c]) for c in flcc.index]
    logger.log('Recomputing arbolate sums from minor distributaries')

    # cull flow paths below minor distributaries
    # until they reach the arbolate sum threshold
    logger.statement('Culling minor distributary flowlines < {} km from divergence...'.format(asum_thresh))
    to_drop = flcc.loc[flcc.asum_calc < asum_thresh, :].index
    flcc.drop(to_drop, axis=0, inplace=True)

    flcc['asum_diff'] = flcc.nhd_asum - flcc.asum_calc

    # estimate channel width using arbolate sum relationship
    logger.statement('Populating channel widths...')
    logger.statement('width = {} * arbolate sum (meters) ^ {}'.format(width_from_asum_a_param,
                                                                      width_from_asum_b_param))
    flcc['width1asum'] = width_from_arbolate_sum(flcc['asum_calc'].values - flcc['LENGTHKM'].values,
                                                 a=width_from_asum_a_param,
                                                 b=width_from_asum_b_param,
                                                 minimum_width=minimum_width,
                                                 input_units='km', output_units=output_length_units)
    flcc['width2asum'] = width_from_arbolate_sum(flcc['asum_calc'].values,
                                                 a=width_from_asum_a_param,
                                                 b=width_from_asum_b_param,
                                                 minimum_width=minimum_width,
                                                 input_units='km', output_units=output_length_units)

    if narwidth_shapefile is not None:
        if not os.path.exists(narwidth_shapefile):
            raise IOError("narwidth_shapefile: {} not found!".format(narwidth_shapefile))
        # sample widths for wider streams from NARWidth
        logger.log('Sampling widths from NARWidth database')
        logger.log_package_version('rtree')
        narwidth_crs = CRS(prjfile=narwidth_shapefile[:-4] + '.prj')
        narwidth_bounds = project(flowline_bounds, flowline_crs.proj_str, narwidth_crs.proj_str)
        sample_NARWidth(flcc, narwidth_shapefile,
                        waterbodies=waterbody_shapefiles,
                        filter=narwidth_bounds,
                        flowlines_epsg=project_crs.epsg,
                        output_width_units=output_length_units)
        logger.log('Sampling widths from NARWidth database')
        flcc['width1'] = flcc.width1asum
        flcc['width2'] = flcc.width2asum
        frac_narwidth = np.sum(~np.isnan(flcc.narwd_mean))/len(flcc)
        logger.statement('Flowline widths estimated from arbolate sum: {0:.1%}'.format(1-frac_narwidth), log_time=False)
        logger.statement('Flowline widths sampled from NARWidth: {0:.1%}'.format(frac_narwidth), log_time=False)
        flcc.loc[~np.isnan(flcc.narwd_mean), 'width1'] = flcc.loc[~np.isnan(flcc.narwd_mean), 'narwd_mean']
        flcc.loc[~np.isnan(flcc.narwd_mean), 'width2'] = flcc.loc[~np.isnan(flcc.narwd_mean), 'narwd_mean']

    logger.log('Preprocessing Flowlines')

    return flcc


def clip_flowlines_to_polygon(flowlines, polygon_shapefile,
                              simplify_tol=100, logger=None):
    """Clip line features in a flowlines DataFrame to polygon
    features in polygon_shapefile.

    Parameters
    ----------
    flowlines : DataFrame
        Output from :func:`~sfrmaker.preprocessing.preprocess_nhdplus`
    polygon_shapefile : str
        Shapefile of model active area, in same CRS as flowlines
    simplify_tol : float
        Simplification tolerance for ``polygon_shapefile`` to speed clipping.
        See :doc:`shapely:manual` for more details.
    logger : Logger instance

    Returns
    -------
    flc : clipped flowlines dataframe
    """

    if logger is None:
        logger = Logger()

    with fiona.open(polygon_shapefile) as src:
        extent_poly_albers = shape(next(src)['geometry'])

    # simplify polygon vertices to speed intersection testing
    # (can be very slow for polygons generated from rasters)
    extent_poly_albers = extent_poly_albers.buffer(simplify_tol).simplify(simplify_tol)

    logger.log('Culling flowlines outside of {}'.format(polygon_shapefile))
    lines = flowlines.geometry.tolist()
    print('starting lines: {:,d}'.format(len(lines)))
    intersects = [g.intersects(extent_poly_albers) for g in lines]
    flc = flowlines.loc[intersects].copy()
    flc['geometry'] = [g.intersection(extent_poly_albers) for g in flc.geometry]
    drop = np.array([g.is_empty for g in flc.geometry.tolist()])
    if len(drop) > 0:
        flc = flc.loc[~drop]
    print('remaining lines: {:,d}'.format(len(flc)))
    logger.log('Culling flowlines outside of {}'.format(polygon_shapefile))
    return flc


def sample_NARWidth(flowlines, narwidth_shapefile, waterbodies,
                    filter=None,
                    flowlines_epsg=None,
                    output_width_units='meters',
                    outpath='shps/'):
    """
    Sample the North American River Width Database by
    doing a spatial join (transfer width information from
    NARWidth shapefile to flowlines shapefile based on proximity).

    Parameters
    ----------
    flowlines : DataFrame
        flowlines dataframe from preprocess_nhdplus().
        Flowlines must be in a projected Coordinate reference system (CRS).
    narwidth_shapefile : str
        Path to shapefile from the NARWidth database (Allen and Pavelsky, 2015).
    waterbody_shapefiles : str or list of strings, optional
        Path(s) to NHDPlus NHDWaterbody shapefile(s). Only required if a
        ``narwidth_shapefile`` is specified.
    flowlines_epsg : int
        EPSG code for Coordinate reference system of flowlines
    filter : tuple
        Bounds (most likely in lat/lon) for filtering NARWidth lines that are read in
        (left, bottom, right, top)
    output_width_units : str, any length unit; e.g. {'m', 'meters', 'ft', etc.}
        Units for width and elevation attribute values included with the output flowlines.
        NARWidth widths are assumed to be in meters.

    Returns
    -------
    This function operates on the fl DataFrame in place.

    Notes
    -----
    To avoid erroneous overlap between main-stem NARWidth estimates and minor tributaries, flowlines with arbolate sums less than 500 km only receive widths from NARWidth lines that have at least 50% of their length inside of the 1-km buffer. NARWidth values are generally higher than arbolate sum-based estimates, because the NARWidth estimates represent mean flows and include all reaches of the stream, whereas the arbolate sum estimates are based on field measurements taken at narrower than average, well-behaved channel sections near stream gages, under base flow conditions. Therefore, measured channel widths may be biased low compared to actual widths throughout the stream network (Allen and Pavelsky, 2015; Park, 1977).
    """

    wb = shp2df(waterbodies)

    if not os.path.isdir(outpath):
        os.makedirs(outpath)

    flowline_crs = CRS(epsg=flowlines_epsg)
    if flowline_crs.pyproj_crs.is_geographic:
        msg = ("Flowlines must be in a projected Coordinate Reference System "
               "(CRS; i.e. with units of meters).")
        raise ValueError(msg)

    # read in narwidth shapefile; reproject to flowline CRS
    nw = shp2df(narwidth_shapefile, filter=filter)
    narwidth_crs = CRS(prjfile=narwidth_shapefile[:-4] + '.prj')
    nw['geometry'] = project(nw.geometry, narwidth_crs.proj_str, flowline_crs.proj_str)

    # draw buffers around flowlines
    buffdist = 1000  # m
    buffers = [g.buffer(buffdist) for g in flowlines.geometry]
    flbuff = flowlines.copy()
    flbuff['geometry'] = buffers
    df2shp(flbuff, '{}/flowlines_edited_buffers_{}.shp'.format(outpath, buffdist), epsg=flowlines_epsg)

    # determine which narwidth segments intersect the flowline buffers
    results = intersect_rtree(nw.geometry.tolist(), flbuff.geometry.tolist())

    # weed out tribs that might have picked up narwidths for main stem
    asum_thresh = 500  # threshold for evaluating whether flowline is a minor distributary
    # (will have small calculated asum)
    overlap_thresh = 0.5  # require lines with small calculated asum to be at least 50% with buffered NARWidth points

    # compile statistics on sampled narwidths
    n = []
    widths_mean = []
    widths_std = []
    widths_min = []
    widths_max = []

    for i, r in enumerate(results):
        fl_info = flowlines.iloc[i]
        # if the calculated asum is less than the threshold
        # do another test to see how much overlap there is
        # between flowling and narwidth
        # goal is to prevent small tribs or distribs from being assigned huge widths
        append_narwidth = False
        if len(r) > 0:
            if fl_info['asum_calc'] < asum_thresh:
                narwidth_line_buffered = MultiLineString(nw.loc[r, 'geometry'].tolist()).buffer(buffdist)
                fl_g = fl_info['geometry']
                fl_overlap = fl_g.intersection(narwidth_line_buffered).length / fl_g.length
                if fl_overlap > overlap_thresh:
                    append_narwidth = True
            else:
                append_narwidth = True

        if append_narwidth:
            n.append(len(r))
            sampled_widths = nw.loc[r, 'width']  # convert from meters to feet
            widths_mean.append(sampled_widths.mean())
            widths_std.append(sampled_widths.std())
            widths_min.append(sampled_widths.min())
            widths_max.append(sampled_widths.max())
        else:
            n.append(np.nan)
            widths_mean.append(np.nan)
            widths_std.append(np.nan)
            widths_min.append(np.nan)
            widths_max.append(np.nan)

    unit_conversion = convert_length_units('meters', output_width_units)
    flowlines['narwd_n'] = n
    flowlines['narwd_mean'] = np.array(widths_mean) * unit_conversion
    flowlines['narwd_std'] = np.array(widths_std) * unit_conversion
    flowlines['narwd_min'] = np.array(widths_min) * unit_conversion
    flowlines['narwd_max'] = np.array(widths_max) * unit_conversion
    waterbodies = set(wb.COMID)
    flowlines['is_wb'] = [True if c in waterbodies else False for c in flowlines.WBAREACOMI]

    flowlines.drop('geometry', axis=1).to_csv('{}/flowlines_w_sampled_narwidth_elevations.csv'.format(outpath))

    # only apply narwidths to rivers that are listed as waterbodies
    # or those that aren't, but have an asum > 500 km, and a sampled value
    rivers_with_widths = ~flowlines.is_wb & (flowlines.nhd_asum > 500) & ~np.isnan(flowlines.narwd_mean)
    wbs = flowlines.is_wb & ~np.isnan(flowlines.narwd_mean)
    criteria = rivers_with_widths | wbs

    df2shp(flowlines.loc[criteria, :], '{}/flowlines_w_sampled_narwidth_elevations.shp'.format(outpath),
           epsg=flowlines_epsg)


def edit_flowlines(flowlines, config_file,
                   id_column='COMID', toid_column='tocomid',
                   logger=None):
    """Make edits to the flowlines in flowlines_file,
    as described in config_file.

    Parameters
    ----------
    flowlines : shapefile or DataFrame
        Flowlines to edit. If a shapefile is specified, a backup
        with ".original" before the extension is made, and the
        input shapefile is overwritten by the results.
    config_file : yaml file
        e.g. 'flowline_edits.yml'
    id_column : str
        Column in flowlines with unique identifiers (e.g. COMIDs)
    toid_column : str
        Column in flowslines with downstream routing connections (identifiers)
    logger : sfrmaker logger instance, optional
        Pass a logger file instance to continue writing to an open logger file
        (e.g. after or before other operations)

    Returns
    -------
    flowlines : DataFrame
        Edited flowlines.
    """

    if logger is None:
        logger = Logger()
    logger.log('editing flowlines...')

    # load the configuration file
    config_path = os.path.abspath(os.path.split(config_file)[0])
    logger.log_file_and_date_modified(config_file)
    with open(config_file) as src:
        cfg = yaml.load(src, Loader=yaml.Loader)

    if isinstance(flowlines, str):
        logger.log_file_and_date_modified(flowlines)
        df = shp2df(flowlines)
        # make a backup
        for ext in '.shp', '.dbf', '.shx', '.prj':
            source = flowlines[:-4] + ext
            dest = flowlines[:-4] + '.original' + ext
            shutil.copy(source, dest)
        prj_file = dest  # for writing a new shapefile at the end
    elif isinstance(flowlines, pd.DataFrame):
        df = flowlines.copy()
    else:
        raise TypeError('Invalid datatype for flowlines input: {}'.format(type(flowlines)))
    df.index = df[id_column]

    if 'add_flowlines' in cfg:
        add_flowlines_file = os.path.join(config_path,
                                          cfg['add_flowlines']['filename'])
        df2 = shp2df(add_flowlines_file)

        # resolve case differences in column names
        # conform columns to flowlines
        column_mappings = {}
        lower_cols = {c.lower(): c for c in df.columns}
        for c2 in df2.columns:
            if c2.lower() in lower_cols:
                column_mappings[c2] = lower_cols[c2.lower()]
        df2.rename(columns=column_mappings, inplace=True)

        # drop the IDs being added if they already exist
        df = df.loc[~df[id_column].isin(df2[id_column])]

        df = df.append(df2)
        df.index = df[id_column]
        logger.statement('added flowlines: {}'.format(textwrap.fill(str(df2[id_column].tolist()),
                                                                    100)))
    drop_upids = None
    if 'drop_flowlines' in cfg:

        drop_ids = cfg['drop_flowlines']
        drop_rows = df.index.isin(drop_ids)
        drop_upids = set(df.loc[drop_rows, toid_column])
        df = df.loc[~drop_rows]
        logger.statement('dropped flowlines: {}'.format(textwrap.fill(str(drop_upids), 100)))

    if 'reroute_flowlines' in cfg:
        for k, v in cfg['reroute_flowlines'].items():
            if k in df.index:
                df.loc[k, toid_column] = v
            else:
                raise KeyError("{} not in {}; can't re-route to {}".format(k, flowlines, v))
            logger.statement('rerouted {} to {}'.format(k, v))

    # verify that all to comids besides 0 are in id column
    # actually apparently don't have to do this because
    # there are already many toids not in the preprocessed flowlines
    # sfrmaker presumably converts them to outlets
    #notin = set(df[toid_column]).difference(df.index)

    # write out an updated version of the input flowlines file
    if isinstance(flowlines, str):
        df2shp(df, flowlines, prj=prj_file)
        logger.statement('wrote {}'.format(flowlines))
    return df