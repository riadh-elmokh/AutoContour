"""Processing pipeline: DEM grid -> contours, spot elevations, report. No QGIS."""
import numpy as np

from . import contour, dem


def process(d, p, is_canceled=None, progress=None):
    """d: dict from demio.read_dem. p: parameters dict. Returns results dict."""
    def cancelled():
        return bool(is_canceled and is_canceled())

    def prog(v):
        if progress:
            progress(v)

    z_raw = dem.to_unit(d["z"], p["dem_unit"], p["out_unit"])
    xs, ys = d["xs"], d["ys"]
    st = dem.stats(z_raw)
    if st is None:
        raise ValueError("The DEM window contains no valid elevation values.")
    res = {"stats": st, "shape": z_raw.shape, "contours": [], "spots": []}
    prog(5)

    # Land DEMs: 0 and below is the sea. Put it just under 0 so the 0 line is the coast, the
    # first polygon starts at 0 and nothing is made over the sea.
    sea = (z_raw <= 0.0) if p.get("sea_out") else None
    z_land = np.where(sea, -1.0, z_raw) if sea is not None else z_raw
    if sea is not None:
        res["sea_pct"] = 100.0 * float(np.count_nonzero(sea)) / sea.size

    if p.get("contours"):
        z = contour.smooth_grid(z_land, p["smooth"]) if p["smooth"] > 0 else z_land
        levels = dem.contour_levels(float(np.nanmin(z)), float(np.nanmax(z)),
                                    p["interval"], p["base"])
        if len(levels) > 5000:
            raise ValueError("%d contour levels: the interval is too small for this "
                             "elevation range." % len(levels))
        for k, lv in enumerate(levels):
            if cancelled():
                return None
            n_idx = max(1, int(p["index_n"]))
            is_idx = int(round((lv - p["base"]) / p["interval"])) % n_idx == 0
            for coords, closed in contour.contour_lines(z, xs, ys, lv):
                if closed and len(coords) < p["min_points"]:
                    continue
                res["contours"].append({"level": lv, "coords": coords,
                                        "closed": closed, "is_index": int(is_idx)})
            prog(5 + 55.0 * (k + 1) / max(len(levels), 1))
    prog(60)

    if p.get("bands"):
        res["bands"] = elevation_bands(z_land if p["smooth"] <= 0 else contour.smooth_grid(z_land, p["smooth"]),
                                       d, p, is_canceled)
        if res["bands"] is None:
            return None
        if sea is not None:
            res["bands"] = [b for b in res["bands"] if b["hi"] > 0.0]
    prog(70)

    if p.get("spots"):
        hi = dem.highest_point(np.where(sea, np.nan, z_raw) if sea is not None else z_raw)
        found = dem.find_spots(z_raw, p["radius"], p["relief"],
                               want_peaks=p["peaks"], want_lows=p["lows"], sea=sea)
        if cancelled():
            return None
        rows = []
        hi_in_list = False
        for i, j, v, kind in found:
            is_hi = hi is not None and (i, j) == (hi[0], hi[1]) and kind == "peak"
            hi_in_list = hi_in_list or is_hi
            rows.append((float(xs[j]), float(ys[i]), v, kind, int(is_hi)))
        if hi is not None and not hi_in_list:
            rows.append((float(xs[hi[1]]), float(ys[hi[0]]), hi[2], "peak", 1))
        for x, y, v, kind, is_hi in rows:
            vv = float(round(v)) if p["round"] else round(v, 2)
            res["spots"].append({"x": x, "y": y, "elev": vv, "type": kind,
                                 "is_highest": is_hi})
        if hi is not None:
            res["highest"] = (float(xs[hi[1]]), float(ys[hi[0]]), hi[2])
    prog(80)

    if p.get("hillshade_path"):
        from . import demio
        geographic = d.get("geographic", p.get("geographic", False))
        dx, dy = d["dx"], d["dy"]
        if geographic:
            lat = float(np.nanmean(ys))
            mdx = dx * 111320.0 * max(np.cos(np.radians(lat)), 0.01)
            mdy = dy * 110540.0
        else:
            mdx, mdy = dx, dy
        zm = dem.to_unit(d["z"], p["dem_unit"], "m")
        shade = dem.hillshade(zm, mdx, mdy)
        demio.write_hillshade(p["hillshade_path"], shade, xs, ys, dx, dy, d["wkt"])
        res["hillshade"] = p["hillshade_path"]
    if p.get("slope_path") or p.get("aspect_path"):
        from . import demio
        geographic = d.get("geographic", p.get("geographic", False))
        if geographic:
            lat = float(np.nanmean(ys))
            mdx = d["dx"] * 111320.0 * max(np.cos(np.radians(lat)), 0.01)
            mdy = d["dy"] * 110540.0
        else:
            mdx, mdy = d["dx"], d["dy"]
        zm = dem.to_unit(d["z"], p["dem_unit"], "m")
        slope, aspect = dem.slope_aspect(zm, mdx, mdy)
        if p.get("slope_path"):
            cls = dem.slope_classes(slope)
            demio.write_hillshade(p["slope_path"], cls, xs, ys, d["dx"], d["dy"], d["wkt"])
            res["slope"] = p["slope_path"]
            tot = max(int((cls > 0).sum()), 1)
            res["slope_share"] = [(lab, 100.0 * int((cls == k).sum()) / tot)
                                  for k, (_a, _b, lab, _c) in enumerate(dem.SLOPE_CLASSES, 1)]
            res["slope_max"] = float(np.nanmax(slope))
            res["slope_mean"] = float(np.nanmean(slope))
        if p.get("aspect_path"):
            demio.write_hillshade(p["aspect_path"], dem.aspect_classes(aspect, slope),
                                  xs, ys, d["dx"], d["dy"], d["wkt"])
            res["aspect"] = p["aspect_path"]
    prog(95)
    return res


def elevation_bands(z, d, p, is_canceled=None):
    """Polygons between consecutive levels (0-100, 100-200, ...) using GDAL's polygon
    contouring. Returns a list of dicts {lo, hi, wkb} or None if cancelled."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    zmin, zmax = float(np.nanmin(z)), float(np.nanmax(z))
    itv, base = float(p["interval"]), float(p["base"])
    first = np.floor((zmin - base) / itv) * itv + base
    last = np.ceil((zmax - base) / itv) * itv + base
    levels = [float(first + k * itv) for k in range(int(round((last - first) / itv)) + 1)]
    if len(levels) > 1500:
        raise ValueError("%d bands: the interval is too small for this elevation range." % len(levels))
    nr, nc = z.shape
    dx, dy = d["dx"], d["dy"]
    mem = "/vsimem/ac_bands.tif"
    nod = -32768.0
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(mem, nc, nr, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((float(d["xs"][0] - dx / 2.0), dx, 0.0, float(d["ys"][0] + dy / 2.0), 0.0, -dy))
    if d.get("wkt"):
        ds.SetProjection(d["wkt"])
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nod)
    band.WriteArray(np.where(np.isfinite(z), z, nod).astype(np.float32))
    band.FlushCache()
    srs = osr.SpatialReference()
    if d.get("wkt"):
        srs.ImportFromWkt(d["wkt"])
    out = ogr.GetDriverByName("Memory").CreateDataSource("bands")
    lyr = out.CreateLayer("b", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("lo", ogr.OFTReal))
    lyr.CreateField(ogr.FieldDefn("hi", ogr.OFTReal))
    try:
        gdal.ContourGenerateEx(band, lyr, options=[
            "FIXED_LEVELS=" + ",".join("%.6f" % v for v in levels),
            "POLYGONIZE=YES", "ID_FIELD=0", "ELEV_FIELD_MIN=1", "ELEV_FIELD_MAX=2",
            "NODATA=%s" % nod])
    finally:
        ds = None
        gdal.Unlink(mem)
    rows = []
    for f in lyr:
        if is_canceled and is_canceled():
            return None
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        lo, hi = f.GetField("lo"), f.GetField("hi")
        lo = lo if lo is not None else first
        hi = hi if hi is not None else last
        rows.append({"lo": float(lo), "hi": float(hi), "wkb": bytes(g.ExportToWkb())})
    out = None
    return rows


def report_text(name, d, p, res, unit_label):
    st = res["stats"]
    lines = [
        "AutoContour report",
        "DEM: %s" % name,
        "Cells: %d x %d (%d)" % (res["shape"][1], res["shape"][0], res["shape"][0] * res["shape"][1]),
        "Cell size: %.6g x %.6g (CRS units)" % (d["dx"], d["dy"]),
        "DEM vertical unit: %s   Output unit: %s" % (p["dem_unit"], p["out_unit"]),
        "Minimum elevation: %.1f %s" % (st["min"], unit_label),
        "Maximum elevation: %.1f %s" % (st["max"], unit_label),
        "No-data cells: %.2f %%" % st["nodata_pct"],
    ]
    if p.get("contours"):
        lines.append("Contours: %d lines, interval %g %s, index every %d, smoothing passes %d"
                     % (len(res["contours"]), p["interval"], unit_label, p["index_n"], p["smooth"]))
    if res.get("slope_share"):
        lines.append("Slope: mean %.1f°, maximum %.1f°. Share of the area: %s" % (
            res["slope_mean"], res["slope_max"],
            ", ".join("%s %.0f %%" % (lab, pc) for lab, pc in res["slope_share"])))
    if res.get("sea_pct") is not None:
        lines.append("Sea left out: %.1f %% of the window is at or below 0 (the 0 line is the coast)."
                     % res["sea_pct"])
    if p.get("bands"):
        lines.append("Elevation bands: %d polygons, every %g %s"
                     % (len(res.get("bands") or []), p["interval"], unit_label))
    if p.get("spots"):
        peaks = sum(1 for s in res["spots"] if s["type"] == "peak")
        lows = sum(1 for s in res["spots"] if s["type"] == "low")
        lines.append("Spot elevations: %d peaks, %d lows (radius %d cells, minimum relief %g %s)"
                     % (peaks, lows, p["radius"], p["relief"], unit_label))
        if "highest" in res:
            hx, hy, hv = res["highest"]
            lines.append("Highest elevation: %.1f %s at %.5f, %.5f" % (hv, unit_label, hx, hy))
    if st["nodata_pct"] > 0.5:
        lines.append("WARNING: the DEM has voids (no-data). Contours stop at voids and "
                     "spot elevations next to them may be wrong.")
    lines.append("Results come from a DEM, not from survey data. Check them against the "
                 "AIP and official sources before using them on an official chart.")
    return "\n".join(lines)
