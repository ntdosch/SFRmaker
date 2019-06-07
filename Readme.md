SFRmaker
===
SFRmaker is a python package for automating construction of stream flow routing networks from hydrography data. Hydrography are input from a polyline shapefile and intersected with a structured grid defined using a Flopy `SpatialReference` instance. Attribute data are supplied via `.dbf` files (NHDPlus input option) or via specified fields in the hydrography shapefile. Line fragments representing intersections between the flowlines and model grid cells are converted to SFR reaches using the supplied attribute data. MODFLOW-NWT/2005 or MODFLOW-6 SFR package input can then be written, along with shapefiles for visualizing the SFR package dataset.


### Version 0.1
[![Build Status](https://travis-ci.com/aleaf/SFRmaker.svg?branch=master)](https://travis-ci.com/aleaf/SFRmaker)
[![Coverage Status](https://codecov.io/github/aleaf/SFRmaker/coverage.svg?branch=master)](https://codecov.io/github/aleaf/SFRmaker/coverage.svg?branch=master)


Getting Started
----------------------------------------------- 

```python
import flopy
import sfrmaker
```
#### create an instance of the lines class from NHDPlus data 
* alternatively, **`lines`** can also be created from a shapefile or dataframe containing LineString features representing streams

```python
lns = sfrmaker.lines.from_NHDPlus_v2(NHDFlowlines='NHDFlowlines.shp',  
                            			PlusFlowlineVAA='PlusFlowlineVAA.dbf',  
                            			PlusFlow='PlusFlow.dbf',  
                            			elevslope='elevslope.dbf',  
                            			filter='data/grid.shp')
```
#### create an instance of `lines` from a hydrography shapefile
* when creating `lines` from a shapefile or dataframe, attribute field or column names can be supplied in lieu of the NHDPlus attribute tables (.dbf files).


```python
lns = lines.from_shapefile(flowlines_file,
                           id_column='COMID',
                           routing_column='tocomid',
                           width1_column='width1',
                           width2_column='width2',
                           up_elevation_column='elevupsmo',
                           dn_elevation_column='elevdnsmo',
                           name_column='GNIS_NAME',
                           attr_length_units='feet',
                           attr_height_units='feet')
```
                     
#### create a flopy `SpatialReference` instance defining the model grid

```python

sr = flopy.utils.SpatialReference(delr=np.ones(160)*250,
                                  delc=np.ones(112)*250,
                                  lenuni=1,
                                  xll=682688, yll=5139052, rotation=0,
                                  proj_str='+init=epsg:26715')
```

#### intersect the lines with the model grid
* results in an **`sfrdata`** class instance

```python
sfr = lns.to_sfr(sr=sr)
```

#### write a sfr package file

```python
sfr.write_package('model.sfr')
```
#### write a MODFLOW 6 SFR package file:

```python
sfr.write_package('model.sfr6', version='mf6')
```
#### write shapefiles for visualizing the SFR package
```python
sfr.export_cells('sfr_cells.shp')
sfr.export_outlets('sfr_outlets.shp')
sfr.export_transient_variable('flow', 'sfr_inlets.shp') # inflows to SFR network
sfr.export_lines('sfr_lines.shp')
sfr.export_routing('sfr_routing.shp')
```

Installation
-----------------------------------------------

**Python versions:**

SFRmaker requires **Python** 3.6 (or higher)

**Dependencies:**  
pyyaml  
numpy  
pandas  
fiona  
rasterio  
shapely  
pyproj  
rtree    
flopy  

### Install to site_packages folder
```
python setup.py install
```
### Install in current location (to current python path)
(i.e., for development)  

```  
pip install -e .
```

Input data requirements
-----------------------------------------------


####1) Hydrography data
#####NHDPlus v2 hydrography datasets    
 * Available at <http://www.horizon-systems.com/NHDPlus/NHDPlusV2_data.php>
 * Archives needed/relevant files:
 	* **NHDPlusV21\_XX\_YY\_NHDSnapshot_**.7z**   
 		* NHDFcode.dbf  
 		* NHDFlowline.dbf, .prj, .shp, .shx  
 	* **NHDPlusV21\_XX\_YY\_NHDPlusAttributes\_**.7z**  
 		* elevslope.dbf  
		* PlusFlow.dbf  
		* PlusFlowlineVAA.dbf
	* If your model domain encompasses multiple drainage areas, each type of NHDPlus file (e.g. NHDFlowline.shp, PlusFlow.dbf, etc.) can be supplied as a list. e.g.   
	
		```python
		NHDFlowlines=['<path to drainage area 1>/NHDFlowlines.shp',
		              '<path to drainage area 2>/NHDFlowlines.shp'...
		              ]
		
		```
	

	**Notes:**  

	* XX is Drainage Area ID (e.g., GL for Great Lakes) and YY is the Vector Processing Unit (VPU; e.g. 04) in the  above (see NHDPlus 	website for details).  


#####Other hydrography   
Any Polyline shapefile can be supplied in lieu of NHDPlus, but it must have the following columns, as shown in the second example:  

**flowlines\_file**: path to shapefile  
**id\_column**: unique identifier for each polyline  
**routing\_column**: downstream connection (ID), 0 if none  
**width1\_column**: channel width at start of line, in `attr\_length\_units` (optional)  
**width2\_column**: channel width at end of line, in `attr_length_units` (optional)  
**up\_elevation\_column**: streambed elevation at start of line, in `attr_height_units `  
**dn\_elevation\_column**: streambed elevation at end of line, in `attr_height_units `  
**name\_column**: stream name (optional)  
**attr\_length\_units**: channel width units  
**attr\_height\_units**: streambed elevation units  



####2) Model grid information
is supplied by creating a 	[`flopy.utils.SpatialReference`](https://github.com/modflowpy/flopy/blob/develop/flopy/utils/reference.py) instance, as shown in the examples.