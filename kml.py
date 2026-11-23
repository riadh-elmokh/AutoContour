"""Google Earth export: elevation polygons, contours and spot heights as real 3D (KMZ).

Polygons are drawn flat at the top of their elevation band and extruded down to the ground, so the
bands stack like terraces. Lines and points float at their true altitude (absolute mode).
"""
import os
import zipfile

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
)

from .styling import DEFAULT_PALETTE, band_colours

MAX_LINE_POINTS = 400000


def escape(text):
    """Escape text for an XML element (we only write XML, never parse it)."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _abgr(rgb, alpha=255):
    return "%02x%02x%02x%02x" % (alpha, rgb[2], rgb[1], rgb[0])


def _coords(points, alt, nd=6):
    return " ".join("%.*f,%.*f,%.1f" % (nd, x, nd, y, alt) for x, y in points)


def _ring_points(ring):
    return [(p.x(), p.y()) for p in ring]


def _polygons(geom):
    """Yield (exterior, [holes]) as lists of (x, y) from any polygon geometry."""
    if geom.isMultipart():
        polys = geom.asMultiPolygon()
    else:
        polys = [geom.asPolygon()]
    for poly in polys:
        if not poly:
            continue
        yield _ring_points(poly[0]), [_ring_points(r) for r in poly[1:]]


def write_kmz(path, res, crs, unit, palette=DEFAULT_PALETTE, exaggeration=1.0, title="AutoContour"):
    """Write `path` (.kmz). Returns a short text with what was written."""
    to_m = 0.3048 if str(unit).lower().startswith("f") else 1.0
    k = to_m * float(exaggeration)
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    ct = None
    if crs.isValid() and crs != wgs:
        ct = QgsCoordinateTransform(crs, wgs, QgsProject.instance())

    def tx(geom):
        if ct is not None:
            geom.transform(ct)
        return geom

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           "<name>%s</name>" % escape(title),
           '<Style id="idx"><LineStyle><color>ff0b2f5c</color><width>2.6</width></LineStyle></Style>',
           '<Style id="ln"><LineStyle><color>ff16346'
           '0</color><width>1.2</width></LineStyle></Style>',
           '<Style id="peak"><IconStyle><color>ff2828c8</color><scale>1.1</scale><Icon>'
           '<href>http://maps.google.com/mapfiles/kml/shapes/triangle.png</href></Icon></IconStyle>'
           '<LineStyle><color>ccffffff</color></LineStyle></Style>',
           '<Style id="low"><IconStyle><color>ffffffff</color><scale>0.9</scale><Icon>'
           '<href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle>'
           '<LineStyle><color>ccffffff</color></LineStyle></Style>']
    notes = []
    bands = res.get("bands") or []
    if bands:
        cols = band_colours(len(bands), palette)
        out.append("<Folder><name>Elevation bands (3D)</name>")
        for i, (b, rgb) in enumerate(zip(bands, cols)):
            geom = QgsGeometry()
            geom.fromWkb(b["wkb"])
            geom = tx(geom)
            alt = float(b["hi"]) * k
            label = "%g - %g %s" % (b["lo"], b["hi"], unit)
            out.append('<Style id="b%d"><PolyStyle><color>%s</color><fill>1</fill><outline>1</outline>'
                       '</PolyStyle><LineStyle><color>ff506478</color><width>0.8</width></LineStyle></Style>'
                       % (i, _abgr(rgb, 235)))
            parts = []
            for ext, holes in _polygons(geom):
                inner = "".join("<innerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing>"
                                "</innerBoundaryIs>" % _coords(h, alt) for h in holes)
                parts.append("<Polygon><extrude>1</extrude><altitudeMode>absolute</altitudeMode>"
                             "<outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing>"
                             "</outerBoundaryIs>%s</Polygon>" % (_coords(ext, alt), inner))
            if parts:
                out.append("<Placemark><name>%s</name><styleUrl>#b%d</styleUrl>"
                           "<MultiGeometry>%s</MultiGeometry></Placemark>" % (escape(label), i, "".join(parts)))
        out.append("</Folder>")
        notes.append("%d elevation polygons" % len(bands))
    contours = res.get("contours") or []
    if contours:
        total = sum(len(c["coords"]) for c in contours)
        skip = max(1, total // MAX_LINE_POINTS)
        out.append("<Folder><name>Contours (3D)</name>")
        n = 0
        for c in contours:
            pts = c["coords"][::skip]
            if len(pts) < 2:
                continue
            lv = float(c["level"])
            geom = tx(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts]))
            line = [(p.x(), p.y()) for p in geom.asPolyline()]
            idx = int(c.get("is_index", 0))
            out.append("<Placemark>%s<styleUrl>#%s</styleUrl><LineString><altitudeMode>absolute</altitudeMode>"
                       "<coordinates>%s</coordinates></LineString></Placemark>"
                       % ("<name>%g</name>" % lv if idx else "", "idx" if idx else "ln",
                          _coords(line, lv * k + 2.0, 6)))
            n += 1
        out.append("</Folder>")
        notes.append("%d contours" % n)
    spots = res.get("spots") or []
    if spots:
        out.append("<Folder><name>Peaks and low points</name>")
        for s in spots:
            geom = tx(QgsGeometry.fromPointXY(QgsPointXY(s["x"], s["y"])))
            p = geom.asPoint()
            kind = "peak" if s["type"] == "peak" else "low"
            name = "%d %s%s" % (round(s["elev"]), unit, " (highest)" if s.get("is_highest") else "")
            out.append("<Placemark><name>%s</name><styleUrl>#%s</styleUrl><Point><extrude>1</extrude>"
                       "<altitudeMode>absolute</altitudeMode><coordinates>%.6f,%.6f,%.1f</coordinates>"
                       "</Point></Placemark>" % (escape(name), kind, p.x(), p.y(), float(s["elev"]) * k + 3.0))
        out.append("</Folder>")
        notes.append("%d points" % len(spots))
    out.append("</Document></kml>")
    kml = "\n".join(out)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
    return ", ".join(notes) or "nothing to export"
