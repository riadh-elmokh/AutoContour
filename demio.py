"""Read DEM windows with GDAL (osgeo ships with QGIS) and optional download."""
import contextlib
import math
import os

import numpy as np

MAX_CELLS = 25000000
TARGET_CELLS = 16000000
TILE_LIMIT = 4
COPERNICUS = ("https://copernicus-dem-30m.s3.amazonaws.com/"
              "{name}/{name}.tif")


def _gdal():
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("GDAL (osgeo) is not available in this QGIS.") from exc
    gdal.UseExceptions()
    return gdal


def read_dem(path, bbox=None):
    """Read band 1 as float64 (NaN = no data), clipped to bbox
    (xmin, ymin, xmax, ymax) in the DEM's own CRS. North-up rasters only."""
    gdal = _gdal()
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError("Cannot open the DEM: %s" % path)
    gt = ds.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0 or gt[5] >= 0 or gt[1] <= 0:
        raise RuntimeError("Only north-up rasters (no rotation) are supported.")
    nc, nr = ds.RasterXSize, ds.RasterYSize
    col0, col1, row0, row1 = 0, nc, 0, nr
    if bbox is not None:
        xmin, ymin, xmax, ymax = bbox
        col0 = max(0, int(math.floor((xmin - gt[0]) / gt[1])))
        col1 = min(nc, int(math.ceil((xmax - gt[0]) / gt[1])))
        row0 = max(0, int(math.floor((ymax - gt[3]) / gt[5])))
        row1 = min(nr, int(math.ceil((ymin - gt[3]) / gt[5])))
    if col1 - col0 < 3 or row1 - row0 < 3:
        raise RuntimeError("The chosen extent does not overlap the DEM (or is smaller than 3 cells).")
    if (col1 - col0) * (row1 - row0) > MAX_CELLS:
        raise RuntimeError(
            "The DEM window has %d cells (limit %d). Use a smaller extent or "
            "a coarser DEM." % ((col1 - col0) * (row1 - row0), MAX_CELLS))
    band = ds.GetRasterBand(1)
    z = band.ReadAsArray(col0, row0, col1 - col0, row1 - row0).astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        z[z == nodata] = np.nan
    z[z <= -9000] = np.nan                   # common void values (-9999, -32768)
    xs = gt[0] + (col0 + np.arange(col1 - col0) + 0.5) * gt[1]
    ys = gt[3] + (row0 + np.arange(row1 - row0) + 0.5) * gt[5]
    wkt = ds.GetProjection()
    geographic = False
    if wkt:
        try:
            from osgeo import osr
            sr = osr.SpatialReference()
            sr.ImportFromWkt(wkt)
            geographic = bool(sr.IsGeographic())
        except (ImportError, RuntimeError):
            geographic = False
    ds = None
    return {"z": z, "xs": xs, "ys": ys, "wkt": wkt, "geographic": geographic,
            "dx": float(gt[1]), "dy": float(-gt[5])}


def write_hillshade(path, shade, xs, ys, dx_deg, dy_deg, wkt):
    gdal = _gdal()
    nr, nc = shade.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, nc, nr, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((float(xs[0] - dx_deg / 2.0), dx_deg, 0.0,
                        float(ys[0] + dy_deg / 2.0), 0.0, -dy_deg))
    ds.SetProjection(wkt)
    band = ds.GetRasterBand(1)
    band.WriteArray(shade)
    band.SetNoDataValue(0)
    band.FlushCache()
    ds = None


def _tile_cop(prefix):
    def name(lat, lon):
        return "Copernicus_DSM_COG_%s_%s%02d_00_%s%03d_00_DEM" % (
            prefix, "N" if lat >= 0 else "S", abs(lat), "E" if lon >= 0 else "W", abs(lon))
    return name


def tile_name(lat, lon):
    return _tile_cop("10")(lat, lon)


def _skadi_name(lat, lon):
    return "%s%02d%s%03d" % ("N" if lat >= 0 else "S", abs(lat),
                              "E" if lon >= 0 else "W", abs(lon))


def _skadi_url(lat, lon):
    n = _skadi_name(lat, lon)
    return "/vsigzip//vsicurl/https://s3.amazonaws.com/elevation-tiles-prod/skadi/%s/%s.hgt.gz" % (n[:3], n)


def _usgs_url(lat, lon):
    n = "n%02d%s%03d" % (lat + 1, "w" if lon < 0 else "e", abs(lon))
    return ("/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
            "current/%s/USGS_13_%s.tif" % (n, n))


def _cop_url(bucket, prefix):
    def url(lat, lon):
        name = _tile_cop(prefix)(lat, lon)
        return "/vsicurl/https://%s.s3.amazonaws.com/%s/%s.tif" % (bucket, name, name)
    return url


# key -> label, tile url builder, max tiles, latitude limits, credit
SOURCES = {
    "cop30": {
        "label": "Copernicus GLO-30 (30 m, global)",
        "url": _cop_url("copernicus-dem-30m", "10"), "max_tiles": 3000, "lat": (-90, 90), "native_deg": 1.0 / 3600,
        "credit": "Copernicus DEM GLO-30 \u00a9 DLR e.V. 2010-2014 and \u00a9 Airbus Defence and "
                  "Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA."},
    "cop90": {
        "label": "Copernicus GLO-90 (90 m, global)",
        "url": _cop_url("copernicus-dem-90m", "30"), "max_tiles": 3000, "lat": (-90, 90), "native_deg": 3.0 / 3600,
        "credit": "Copernicus DEM GLO-90 \u00a9 DLR e.V. 2010-2014 and \u00a9 Airbus Defence and "
                  "Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA."},
    "usgs10": {
        "label": "USGS 3DEP (10 m, USA and territories)",
        "url": _usgs_url, "max_tiles": 3000, "lat": (17, 72), "native_deg": 1.0 / 10800,
        "credit": "USGS 3DEP elevation data (public domain), 1/3 arc-second, served by the "
                  "USGS National Map on AWS."},
    "emodnet": {
        "label": "EMODnet Bathymetry (115 m, European seas and coasts, depths + land)",
        "wcs": {"url": "https://ows.emodnet-bathymetry.eu/wcs", "coverage": "emodnet__mean",
                "axes": ("Lat", "Long"), "bbox": (-36.0, 15.0, 43.0, 90.0)},
        "native_deg": 1.0 / 960,
        "credit": "EMODnet Digital Bathymetry (DTM), EMODnet Bathymetry Consortium, CC BY 4.0. "
                  "Negative values are depths."},
    "etopo": {
        "label": "ETOPO1 (1.8 km, whole world, ocean depths + land)",
        "wcs": {"url": "https://gis.ngdc.noaa.gov/arcgis/services/DEM_mosaics/ETOPO1_ice_surface/"
                       "ImageServer/WCSServer", "coverage": "Coverage1",
                "axes": ("y", "x"), "bbox": (-180.0, -90.0, 180.0, 90.0)},
        "native_deg": 1.0 / 60,
        "credit": "ETOPO1 Global Relief Model, NOAA NCEI (public domain). Coarse: for large sea "
                  "areas and overviews. Negative values are depths."},
    "srtm30": {
        "label": "SRTM 1 arc-second (30 m, 56\u00b0S to 60\u00b0N)",
        "url": _skadi_url, "max_tiles": 9, "lat": (-56, 60), "native_deg": 1.0 / 3600,
        "credit": "SRTM data: NASA / USGS (public domain), served by the AWS open data "
                  "'Terrain Tiles' (Mapzen / Tilezen)."},
}


def _check_url(path):
    """Only https sources are allowed."""
    plain = path
    for pre in ("/vsigzip/", "/vsicurl/"):
        plain = plain.replace(pre, "")
    if not plain.startswith("https://"):
        raise RuntimeError("Only https addresses are allowed.")


def _plain_url(path):
    return path.replace("/vsigzip/", "").replace("/vsicurl/", "")


def _open(path, errors=None):
    """Open a tile through GDAL. On failure returns None and appends the reason to errors."""
    gdal = _gdal()
    _check_url(path)
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "30")
    gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "2")
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    try:
        return gdal.Open(path)
    except RuntimeError as exc:
        if errors is not None:
            errors.append(str(exc))
        return None


def _is_missing(msg):
    low = msg.lower()
    return any(s in low for s in ("404", "403", "not found", "forbidden", "not recognized as",
                                  "no such file", "does not exist"))


def _fetch_file(url, dest, is_canceled=None):
    """Download one file with QGIS's own network stack (QGIS proxy, certificates)."""
    from qgis.core import QgsBlockingNetworkRequest
    from .compat import NET_OK
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest
    req = QNetworkRequest(QUrl(url))
    blocker = QgsBlockingNetworkRequest()
    err = blocker.get(req, forceRefresh=True)
    if err != NET_OK:
        raise RuntimeError(blocker.errorMessage() or "network error")
    with open(dest, "wb") as fh:
        fh.write(bytes(blocker.reply().content()))
    return dest


def _open_tile(src, lat, lon, cache_dir, errors, is_canceled=None, log=None):
    """GDAL streaming first; if the network setup blocks it, a plain download."""
    path = src["url"](lat, lon)
    errs = []
    ds = _open(path, errs)
    if ds is not None:
        return ds
    msg = errs[0] if errs else "unknown error"
    # GDAL's direct reading can fail behind proxies or with odd certificates even when the
    # tile exists, so a plain download through QGIS's network stack is always tried too.
    url = _plain_url(path)
    dest = os.path.join(cache_dir, os.path.basename(url))
    try:
        if log:
            log("Tile %d, %d: direct read failed (%s). Trying a plain download..." % (lat, lon, msg.strip()[:60]))
        _fetch_file(url, dest, is_canceled)
        local = ("/vsigzip/" + dest) if dest.endswith(".gz") else dest
        gdal = _gdal()
        return gdal.Open(local)
    except RuntimeError as exc:
        reason = str(exc).strip()[:120]
        if _is_missing(msg) and _is_missing(reason):
            errors.append("tile %d, %d: not available (%s)" % (lat, lon, reason))
        else:
            errors.append("tile %d, %d: %s" % (lat, lon, reason))
        return None


def tiles_for(west, south, east, north):
    out = []
    for lat in range(int(math.floor(south)), int(math.ceil(north))):
        for lon in range(int(math.floor(west)), int(math.ceil(east))):
            out.append((lat, lon))
    return out


def _tiles_checked(key, west, south, east, north):
    src = SOURCES[key]
    tiles = [t for t in tiles_for(west, south, east, north)
             if src["lat"][0] <= t[0] < src["lat"][1]]
    if not tiles:
        raise RuntimeError("This DEM source has no data for that latitude range.")
    if len(tiles) > src["max_tiles"]:
        raise RuntimeError(
            "The extent needs %d one-degree tiles (limit %d for this source). Use a "
            "smaller extent or another source." % (len(tiles), src["max_tiles"]))
    return src, tiles


def check_connection(key, west, south, east, north):
    """Try to open the first tile. Returns (ok, message)."""
    try:
        src, tiles = _tiles_checked(key, west, south, east, north)
    except RuntimeError as exc:
        return False, str(exc)
    for lat, lon in tiles:
        ds = _open(src["url"](lat, lon))
        if ds is not None:
            return True, "Connected: tile %d, %d is readable (%d of %d tiles checked)." % (
                lat, lon, 1, len(tiles))
    return False, ("Could not open any tile. The network may block this server, or the "
                   "area has no data (open sea). Try another source or your own DEM file.")


WCS_CELLS = 4000000     # largest grid asked from a web coverage service


_TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def _extract_tiff(blob):
    """The GeoTIFF from a WCS answer: plain, or the image part of a multipart/mixed reply."""
    if blob[:4] in _TIFF_MAGIC:
        return blob
    if not blob.startswith(b"--"):
        return None
    boundary = blob.split(b"\r\n", 1)[0].split(b"\n", 1)[0].strip()
    for part in blob.split(boundary):
        head, sep, body = part.partition(b"\r\n\r\n")
        if not sep:
            head, sep, body = part.partition(b"\n\n")
        if sep and (b"image/tiff" in head.lower() or body[:4] in _TIFF_MAGIC):
            if body.endswith(b"\r\n"):
                body = body[:-2]
            elif body.endswith(b"\n"):
                body = body[:-1]
            if body[:4] in _TIFF_MAGIC:
                return body
    return None


def download_wcs(key, west, south, east, north, out_path, is_canceled=None, log=None):
    """Get the extent (EPSG:4326) from a WCS 2.0.1 server as a GeoTIFF."""
    from urllib.parse import quote
    src = SOURCES[key]
    cfg = src["wcs"]
    bw, bs, be, bn = cfg["bbox"]
    w, s, e, n = max(west, bw), max(south, bs), min(east, be), min(north, bn)
    if e <= w or n <= s:
        raise RuntimeError("The area is outside the coverage of %s." % src["label"])
    nat = src["native_deg"]
    cells = (e - w) * (n - s) / (nat * nat)
    url = ("%s?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&CoverageId=%s"
           "&SUBSET=%s(%.6f,%.6f)&SUBSET=%s(%.6f,%.6f)&FORMAT=%s"
           % (cfg["url"], quote(cfg["coverage"]), cfg["axes"][0], s, n, cfg["axes"][1], w, e,
              quote("image/tiff")))
    if cells > WCS_CELLS:
        scale = math.sqrt(WCS_CELLS / cells)
        url += "&SCALEFACTOR=%.6f" % scale
        if log:
            log("Large area: asking the server for a reduced grid (about %.0f m cells)."
                % (nat / scale * 111320.0))
    if log:
        log("Requesting %s ..." % src["label"])
    if is_canceled and is_canceled():
        return None
    raw = out_path + ".part"
    _fetch_file(url, raw)
    with open(raw, "rb") as fh:
        blob = fh.read()
    tiff = _extract_tiff(blob)
    if tiff is None:
        text = blob[:600].decode("utf-8", "replace").replace("\n", " ")[:300]
        os.remove(raw)
        raise RuntimeError("The server did not return a GeoTIFF: %s" % text)
    with open(raw, "wb") as fh:
        fh.write(tiff)
    os.replace(raw, out_path)
    return out_path


def download_tiles(key, west, south, east, north, out_path, is_canceled=None, log=None):
    """Cut the extent (EPSG:4326) out of the source's tiles into a GeoTIFF."""
    if "wcs" in SOURCES[key]:
        return download_wcs(key, west, south, east, north, out_path, is_canceled, log)
    gdal = _gdal()
    src, tiles = _tiles_checked(key, west, south, east, north)
    sources, errors = [], []
    cache_dir = os.path.dirname(os.path.abspath(out_path))
    for lat, lon in tiles:
        if is_canceled and is_canceled():
            return None
        ds = _open_tile(src, lat, lon, cache_dir, errors, is_canceled, log)
        if ds is not None:
            sources.append(ds)
        elif log:
            log("No tile at %d, %d: %s" % (lat, lon, errors[-1] if errors else "not available"))
    if not sources:
        missing = all("not available" in e for e in errors)
        if missing:
            raise RuntimeError(
                "No DEM tile was found for this area: it may be open sea, outside the coverage, "
                "or the server is refused by your network (firewall/proxy). Try an area over "
                "land, or another DEM source. Detail: %s" % (errors[0] if errors else "none"))
        raise RuntimeError(
            "Could not read any DEM tile. Reason: %s" % (errors[0] if errors else "unknown"))
    native = src["native_deg"]
    cells = (east - west) * (north - south) / (native * native)
    res = None
    if cells > TARGET_CELLS:
        res = native * math.sqrt(cells / TARGET_CELLS)
        if log:
            log("Large area: the DEM is read at about %.0f m instead of %.0f m so that it stays "
                "workable." % (res * 111320.0, native * 111320.0))
    return _warp(gdal, out_path, sources, (west, south, east, north), "EPSG:4326", res)


def _warp(gdal, out_path, sources, bounds, bounds_srs, res=None):
    kw = {"format": "GTiff", "creationOptions": ["COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]}
    if res:
        kw.update(xRes=res, yRes=res, resampleAlg="average")
    if bounds is not None:
        kw["outputBounds"] = bounds
        kw["outputBoundsSRS"] = bounds_srs
    try:
        res = gdal.Warp(out_path, sources, **kw)
    except RuntimeError as exc:
        raise RuntimeError("Could not build the DEM: %s" % exc) from exc
    if res is None or not os.path.exists(out_path):
        raise RuntimeError("Could not build the DEM.")
    res = None
    return out_path


def mosaic_files(files, out_path, bbox=None, bbox_srs="EPSG:4326"):
    """Merge several local DEM files into one GeoTIFF (optionally cut to bbox)."""
    gdal = _gdal()
    if not files:
        raise RuntimeError("Choose at least one DEM file.")
    for f in files:
        if not os.path.isfile(f):
            raise RuntimeError("File not found: %s" % f)
    return _warp(gdal, out_path, list(files), bbox, bbox_srs)


def _auto_res(gdal, datasets, bbox, log=None):
    """Output cell size (CRS units) when the window would be too big, else None."""
    from osgeo import osr
    vrt = gdal.BuildVRT("", datasets)
    gt = vrt.GetGeoTransform()
    ext = [gt[0], gt[3] + gt[5] * vrt.RasterYSize, gt[0] + gt[1] * vrt.RasterXSize, gt[3]]
    if bbox is not None:
        wkt = vrt.GetProjection()
        w, s, e, n = bbox
        if wkt:
            src = osr.SpatialReference()
            src.ImportFromEPSG(4326)
            dst = osr.SpatialReference()
            dst.ImportFromWkt(wkt)
            for sr in (src, dst):
                with contextlib.suppress(AttributeError):
                    sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            tr = osr.CoordinateTransformation(src, dst)
            pts = [tr.TransformPoint(x, y)[:2] for x, y in ((w, s), (w, n), (e, s), (e, n))]
            w, e = min(p[0] for p in pts), max(p[0] for p in pts)
            s, n = min(p[1] for p in pts), max(p[1] for p in pts)
        ext = [max(ext[0], w), max(ext[1], s), min(ext[2], e), min(ext[3], n)]
    if ext[2] <= ext[0] or ext[3] <= ext[1]:
        raise RuntimeError("The drawn area does not overlap the DEM.")
    cells = (ext[2] - ext[0]) / gt[1] * (ext[3] - ext[1]) / -gt[5]
    if cells <= TARGET_CELLS:
        return None
    res = gt[1] * math.sqrt(cells / TARGET_CELLS)
    if log:
        log("Large DEM window (%.0f million cells): read at a cell size of %.4g instead of %.4g "
            "so that it stays workable." % (cells / 1e6, res, gt[1]))
    return res


def prepare_local(kind, spec, bbox, out_path, log=None):
    """kind: 'local' (list of files), 'layer' (one file path) or 'url' (https GeoTIFF/COG).
    bbox: (w, s, e, n) in EPSG:4326 or None for the whole raster."""
    gdal = _gdal()
    if kind == "url":
        url = spec.strip()
        if not url.startswith("https://"):
            raise RuntimeError("Only https addresses are allowed.")
        paths = ["/vsicurl/" + url]
    else:
        paths = [spec] if isinstance(spec, str) else list(spec)
        if not paths:
            raise RuntimeError("Choose at least one DEM file.")
        for f in paths:
            if not os.path.isfile(f):
                raise RuntimeError("File not found: %s" % f)
    datasets = []
    for p in paths:
        try:
            datasets.append(gdal.Open(p))
        except RuntimeError as exc:
            raise RuntimeError("Cannot open this raster (%s): %s" % (os.path.basename(p), exc)) from exc
    res = _auto_res(gdal, datasets, bbox, log)
    return _warp(gdal, out_path, datasets, bbox, "EPSG:4326", res)


def from_url(url, bbox, out_path):
    """Cut bbox (EPSG:4326) out of an https GeoTIFF / COG."""
    gdal = _gdal()
    url = url.strip()
    if not url.startswith("https://"):
        raise RuntimeError("Only https addresses are allowed.")
    ds = _open("/vsicurl/" + url)
    if ds is None:
        raise RuntimeError("Cannot open this address as a raster. It must be a direct "
                           "https link to a GeoTIFF / COG, and the network must allow it.")
    return _warp(gdal, out_path, [ds], bbox, "EPSG:4326")
