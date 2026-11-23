# AutoContour
Draw an area on the map, choose a DEM (or use your own), click **Create contours**.

- DEM sources: Copernicus GLO-30 / GLO-90, SRTM, USGS 3DEP 10 m (USA), EMODnet and ETOPO1 (sea depths), your own files, a project raster layer, or an online GeoTIFF link.
- 3D view: rotate, zoom and exaggerate the relief, with contours and peaks (built in, no extra install).
- Elevation colours follow the ICAO Annex 4 hypsometric tint guide; the window follows QGIS when it is minimised.
- Outputs carry Z: contours are LineString Z, spot elevations are Point Z, elevation polygons are Polygon Z (Z = elevation, lower limit for a polygon).
- Google Earth: a .kmz is saved with the results; open it in Google Earth to see the elevation polygons extruded in 3D, the contours and the peaks at their real altitude.
- Outputs: contour lines, elevation polygons, spot elevations (peaks, lows, highest point), slope and aspect maps, hillshade, report.
- Click tool: elevation at any point.
- Large areas are read at a reduced resolution automatically.

Results come from a DEM and are not survey data. License: GPL-2.0.
