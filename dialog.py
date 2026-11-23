import contextlib
import math
import os

import numpy as np
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsGeometry, QgsPoint, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings,
)
from qgis.gui import (
    QgsFileWidget, QgsMapLayerComboBox, QgsMapToolEmitPoint, QgsMapToolExtent, QgsRubberBand,
)
from qgis.PyQt.QtCore import Qt, pyqtSlot
from qgis.PyQt.QtGui import QColor, QIcon, QKeySequence
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QApplication, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

try:
    from qgis.PyQt.QtWidgets import QShortcut
except ImportError:  # Qt6 moved it
    from qgis.PyQt.QtGui import QShortcut

from . import auto, dem, demio, layers, pipeline
from .card import ElevationCard, dms
from .compat import GEOM_POLYGON, MSG_INFO
from .styling import DEFAULT_PALETTE, style_bands, style_classes, style_contours, style_spots
from .task import FuncTask

SETTINGS = "autocontour/"
WGS84 = "EPSG:4326"
DISCLAIMER = (
    "Results come from a DEM and are not survey data. For official charts use "
    "the authority's data and check results before use. Not affiliated with or "
    "endorsed by any aviation authority.")
SOURCE_CHOICES = [
    ("auto", "Automatic (best source that fits your area)"),
    ("cop30", "Copernicus GLO-30 (30 m, global)"),
    ("cop90", "Copernicus GLO-90 (90 m, global, for large areas)"),
    ("usgs10", "USGS 3DEP (10 m, USA and territories)"),
    ("emodnet", "EMODnet Bathymetry (115 m, Europe, sea depths + land)"),
    ("etopo", "ETOPO1 (1.8 km, world, ocean depths + land)"),
    ("srtm30", "SRTM 1 arc-second (30 m, 56°S to 60°N)"),
    ("local", "My own DEM file(s) (client data, drone DEM...)"),
    ("layer", "A raster layer already in the project"),
    ("url", "An online GeoTIFF / COG link (any open data)"),
]
DOWNLOADS = ("auto", "cop30", "cop90", "usgs10", "srtm30", "emodnet", "etopo")
INTERVALS = ["Automatic", "0.25", "0.5", "1", "2", "5", "10", "20", "25", "50", "100", "200", "250", "500", "1000"]


class AutoContourDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.task = None
        self._resetting = False
        self.area = None                 # (west, south, east, north) in EPSG:4326
        self.band = None                 # rubber band showing the chosen area
        self.draw_tool = None
        self.probe_tool = None
        self.probe_marker = None
        self.probe_rl = None
        self._probe_name = "DEM"
        self._probe_data = None
        self.card = None
        self.last_view = None            # what the 3D view needs
        self.viewer = None
        self.last_dem = None             # (path, unit of the file) of the last DEM made
        self._prev_tool = None
        self._esc = None
        self.setWindowTitle("AutoContour: draw an area, get contours")
        icon_file = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(580, 760)
        self.setSizeGripEnabled(True)
        s = QgsSettings()
        root = QVBoxLayout(self)

        # 1. area ----------------------------------------------------------
        g1 = QGroupBox("1. Area")
        v1 = QVBoxLayout(g1)
        row = QHBoxLayout()
        self.draw_btn = QPushButton("Draw area on the map")
        self.draw_btn.clicked.connect(self.start_draw)
        self.view_btn = QPushButton("Use the current map view")
        self.view_btn.clicked.connect(self.use_view)
        self.clear_btn = QPushButton("Clear area")
        self.clear_btn.clicked.connect(self.clear_area)
        row.addWidget(self.draw_btn)
        row.addWidget(self.view_btn)
        row.addWidget(self.clear_btn)
        v1.addLayout(row)
        self.area_lbl = QLabel("No area yet. Draw a rectangle on the map.")
        self.area_lbl.setWordWrap(True)
        self.area_lbl.setMinimumHeight(70)
        self.area_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        v1.addWidget(self.area_lbl)
        root.addWidget(g1)

        # 2. DEM -----------------------------------------------------------
        g2 = QGroupBox("2. DEM type")
        v2 = QVBoxLayout(g2)
        self.source = QComboBox()
        for _key, label in SOURCE_CHOICES:
            self.source.addItem(label)
        self.source.currentIndexChanged.connect(self._update_info)
        v2.addWidget(self.source)
        self.local_files = QgsFileWidget()
        self.local_files.setStorageMode(QgsFileWidget.StorageMode.GetMultipleFiles)
        self.local_files.setFilter("DEM rasters (*.tif *.tiff *.img *.asc *.hgt *.vrt *.dem *.bil);;All files (*)")
        self.layer_box = QgsMapLayerComboBox()
        try:
            from qgis.core import QgsMapLayerProxyModel
            try:
                self.layer_box.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
            except AttributeError:
                self.layer_box.setFilters(QgsMapLayerProxyModel.RasterLayer)
        except ImportError:
            pass
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://.../dem.tif   (direct link to a GeoTIFF or COG)")
        self.file_unit = QComboBox()
        self.file_unit.addItems(["Elevations in the file are in metres", "Elevations in the file are in feet"])
        for w_ in (self.local_files, self.layer_box, self.url_edit, self.file_unit):
            v2.addWidget(w_)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.credit = QLabel("")
        self.credit.setWordWrap(True)
        self.credit.setStyleSheet("color: #666;")
        v2.addWidget(self.credit)
        root.addWidget(g2)

        # 3. options -------------------------------------------------------
        g3 = QGroupBox("3. Options (the defaults are fine)")
        f = QFormLayout(g3)
        f.setVerticalSpacing(8)
        self.interval = QComboBox()
        self.interval.addItems(INTERVALS)
        self.interval.setEditable(True)
        self.unit = QComboBox()
        self.unit.addItems(["Metres", "Feet"])
        self.unit.currentIndexChanged.connect(self._update_info)
        f.addRow("Interval (contours and polygons)", self.interval)
        f.addRow("Elevation unit", self.unit)
        self.sea_out = QCheckBox("Leave the sea out (0 and below is sea: gives the 0 line, polygons start at 0)")
        self.sea_out.setChecked(True)
        self.contours = QCheckBox("Contour lines")
        self.contours.setChecked(True)
        self.bands = QCheckBox("Elevation polygons (0-100, 100-200, ... one polygon per band)")
        self.spots = QCheckBox("Spot elevations: peaks and the highest point")
        self.spots.setChecked(True)
        self.lows = QCheckBox("Also low points (pits and depressions)")
        self.lows.setChecked(True)
        self.spots.toggled.connect(self.lows.setEnabled)
        self.hill = QCheckBox("Hillshade (to check the DEM)")
        self.slope = QCheckBox("Slope map (0-5°, 5-15°, 15-30°, 30-45°, over 45°)")
        self.aspect = QCheckBox("Aspect map (which way the slopes face: N, NE, E...)")
        f.addRow(self.sea_out)
        f.addRow(self.contours)
        f.addRow(self.bands)
        f.addRow(self.spots)
        f.addRow(self.lows)
        self.kml = QCheckBox("Also save a Google Earth file (.kmz) with 3D polygons, lines and points")
        self.kml.setChecked(True)
        self.kml.setToolTip("Open the .kmz in Google Earth: elevation polygons are extruded in 3D, "
                            "contours and spot heights float at their real altitude.")
        f.addRow(self.kml)
        self.kml_exag = QDoubleSpinBox()
        self.kml_exag.setRange(1.0, 10.0)
        self.kml_exag.setSingleStep(0.5)
        self.kml_exag.setValue(1.0)
        self.kml_exag.setSuffix(" ×")
        self.kml_exag.setToolTip("Vertical exaggeration of the Google Earth file. "
                                 "Use 1 × unless the relief is very flat.")
        self.kml.toggled.connect(self.kml_exag.setEnabled)
        f.addRow("Google Earth exaggeration", self.kml_exag)
        f.addRow(self.hill)
        f.addRow(self.slope)
        f.addRow(self.aspect)
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        default = os.path.join(os.path.expanduser("~"), "AutoContour")
        self.folder.setFilePath(s.value(SETTINGS + "folder", default))
        f.addRow("Save in folder", self.folder)
        self.fmt = QComboBox()
        self.fmt.addItems(["GeoPackage", "Shapefile"])
        self.fmt.setCurrentIndex(int(s.value(SETTINGS + "fmt", 0)))
        f.addRow("Format", self.fmt)
        root.addWidget(g3)

        # run ----------------------------------------------------------------
        h = QHBoxLayout()
        self.run_btn = QPushButton("Create contours")
        self.run_btn.setMinimumHeight(38)
        self.run_btn.setStyleSheet("font-weight: bold;")
        self.run_btn.clicked.connect(self.run)
        self.probe_btn = QPushButton("Elevation at click")
        self.probe_btn.setToolTip("Click on the map to read the DEM elevation (uses the last DEM made, "
                                  "or the raster layer chosen above). Esc to stop.")
        self.probe_btn.clicked.connect(self.toggle_probe)
        self.view3d_btn = QPushButton("View in 3D")
        self.view3d_btn.setToolTip("Open the last result (surface, contours, peaks and lows) in a 3D view.")
        self.view3d_btn.setEnabled(False)
        self.view3d_btn.clicked.connect(self.open_3d)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        h.addWidget(self.run_btn, 3)
        h.addWidget(self.probe_btn, 2)
        h.addWidget(self.view3d_btn, 2)
        h.addWidget(self.cancel_btn, 1)
        root.addLayout(h)
        self.bar = QProgressBar()
        root.addWidget(self.bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        root.addWidget(self.log_view)
        note = QLabel(DISCLAIMER)
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        root.addWidget(note)
        self._source_changed()

    # ------------------------------------------------------------ helpers
    def log(self, text):
        self.log_view.appendPlainText(text)

    def _choice(self):
        return SOURCE_CHOICES[self.source.currentIndex()][0]

    def _unit(self):
        return "ft" if self.unit.currentIndex() == 1 else "m"

    def _fmt(self):
        return "gpkg" if self.fmt.currentIndex() == 0 else "shp"

    def _busy(self, on):
        self.run_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        self.draw_btn.setEnabled(not on)
        self.view_btn.setEnabled(not on)
        self.clear_btn.setEnabled(not on)

    @pyqtSlot(float)
    def _on_progress(self, value):
        self.bar.setValue(int(value))

    # --------------------------------------------------------------- area
    def _canvas(self):
        return self.iface.mapCanvas()

    def _to_wgs84(self, rect):
        src = self._canvas().mapSettings().destinationCrs()
        dst = QgsCoordinateReferenceSystem(WGS84)
        if src != dst:
            rect = QgsCoordinateTransform(src, dst, QgsProject.instance()).transformBoundingBox(rect)
        return rect

    def _set_area(self, rect_wgs84):
        w, s, e, n = (rect_wgs84.xMinimum(), rect_wgs84.yMinimum(),
                      rect_wgs84.xMaximum(), rect_wgs84.yMaximum())
        w, e = max(-180.0, w), min(180.0, e)
        s, n = max(-90.0, s), min(90.0, n)
        self.area = (w, s, e, n)
        self._show_band(QgsRectangle(w, s, e, n))
        self._update_info()

    def _probe_layer(self):
        if self.probe_rl is not None and self.probe_rl.isValid():
            return self.probe_rl, self._probe_unit
        if self.last_dem and os.path.exists(self.last_dem[0]):
            rl = QgsRasterLayer(self.last_dem[0], "probe")
            if rl.isValid():
                self.probe_rl, self._probe_unit = rl, self.last_dem[1]
                self._probe_name = self.last_dem[2]
                return rl, self._probe_unit
        if self._choice() == "layer" and self.layer_box.currentLayer() is not None:
            unit = "ft" if self.file_unit.currentIndex() == 1 else "m"
            self._probe_name = self.layer_box.currentLayer().name()
            return self.layer_box.currentLayer(), unit
        return None, "m"

    def toggle_probe(self):
        canvas = self._canvas()
        if self.probe_tool is not None and canvas.mapTool() is self.probe_tool:
            self._stop_probe()
            return
        rl, _unit = self._probe_layer()
        if rl is None:
            QMessageBox.information(
                self, "AutoContour",
                "No DEM yet. Create contours first (the DEM is kept), or choose a raster layer "
                "above ('A raster layer already in the project').")
            return
        if self.probe_tool is None:
            self.probe_tool = QgsMapToolEmitPoint(canvas)
            self.probe_tool.canvasClicked.connect(self._probed)
        self._prev_tool = canvas.mapTool()
        canvas.setMapTool(self.probe_tool)
        if self._esc is None:
            self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.iface.mainWindow())
            self._esc.activated.connect(self._cancel_draw)
        self._esc.setEnabled(True)
        self.probe_btn.setText("Stop (Esc)")
        self.iface.messageBar().pushMessage(
            "AutoContour", "Click on the map to read the elevation. Esc to stop.",
            level=MSG_INFO, duration=4)
        canvas.setFocus()
        self.showMinimized()

    def _stop_probe(self):
        canvas = self._canvas()
        if self._esc is not None:
            self._esc.setEnabled(False)
        if self.probe_tool is not None and canvas.mapTool() is self.probe_tool:
            if self._prev_tool is not None:
                canvas.setMapTool(self._prev_tool)
            else:
                canvas.unsetMapTool(self.probe_tool)
        self._prev_tool = None
        if self.probe_marker is not None:
            canvas.scene().removeItem(self.probe_marker)
            self.probe_marker = None
        if self.card is not None:
            self.card.dismiss()
        self.probe_btn.setText("Elevation at click")
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _probe_info(self, rl, unit, pt, wgs):
        """Elevation, slope, aspect and local relief around a point of the DEM layer."""
        import numpy as np
        px, py = rl.rasterUnitsPerPixelX(), rl.rasterUnitsPerPixelY()
        if rl.crs().isGeographic():
            mx = px * 111320.0 * max(math.cos(math.radians(pt.y())), 0.01)
            my = py * 110540.0
        else:
            mx, my = px, py
        half = 8
        n = 2 * half + 1
        ext = QgsRectangle(pt.x() - n * px / 2.0, pt.y() - n * py / 2.0,
                           pt.x() + n * px / 2.0, pt.y() + n * py / 2.0)
        blk = rl.dataProvider().block(1, ext, n, n)
        arr = np.full((n, n), np.nan)
        for r in range(n):
            for c in range(n):
                if not blk.isNoData(r, c):
                    v = blk.value(r, c)
                    if v == v and v > -9000:
                        arr[r, c] = v
        if unit == "ft":
            arr = arr * 0.3048
        out = {"lat": wgs.y(), "lon": wgs.x(), "cell_m": (mx + my) / 2.0, "slope": None,
               "aspect": None, "slope_pct": 0.0, "relief": None, "radius_m": half * mx,
               "elev_m": None, "elev_ft": None, "kind": "", "note": ""}
        z = arr[half, half]
        if z != z:
            return out
        out["elev_m"], out["elev_ft"] = float(z), float(z) / 0.3048
        if z < 0:
            out["kind"] = "Below sea level (depth)"
        elif z == 0:
            out["kind"] = "At sea level"
        win = arr[half - 1:half + 2, half - 1:half + 2]
        if np.isfinite(win).all():
            s, a = dem.slope_aspect(win, mx, my)
            out["slope"], out["aspect"] = float(s[1, 1]), float(a[1, 1])
            out["slope_pct"] = math.tan(math.radians(out["slope"])) * 100.0
        if np.isfinite(arr).any():
            out["relief"] = float(np.nanmax(arr) - np.nanmin(arr))
        return out

    def _probed(self, point, _button=None):
        rl, unit = self._probe_layer()
        if rl is None:
            return
        canvas = self._canvas()
        src = canvas.mapSettings().destinationCrs()
        pt = QgsPointXY(point)
        if src != rl.crs():
            pt = QgsCoordinateTransform(src, rl.crs(), QgsProject.instance()).transform(pt)
        wgs = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem(WGS84),
                                     QgsProject.instance()).transform(QgsPointXY(point))
        info = self._probe_info(rl, unit, pt, wgs)
        info["source"] = self._probe_name.split("(")[0].replace("DEM:", "").strip()[:26]
        if self.card is None:
            self.card = ElevationCard(canvas)
            self.card.copy_requested.connect(self._copy_probe)
            self.card.plugin_requested.connect(self._stop_probe)
            self.card.user_closed.connect(self._stop_probe)
        self._probe_data = info
        px = canvas.mapSettings().mapToPixel().transform(QgsPointXY(point))
        self.card.show_info(info, QgsPointXY(point), px)
        if info["elev_m"] is not None:
            self.log("Elevation %.1f m (%.0f ft) at %.5f, %.5f" % (
                info["elev_m"], info["elev_ft"], info["lat"], info["lon"]))

    def _copy_probe(self):
        i = getattr(self, "_probe_data", None)
        if not i or i["elev_m"] is None:
            return
        text = ("Elevation %.1f m (%.0f ft)\nLat %s\nLon %s\nSlope %s\nSource %s" % (
            i["elev_m"], i["elev_ft"], dms(i["lat"], True), dms(i["lon"], False),
            "%.1f deg" % i["slope"] if i["slope"] is not None else "-", i["source"]))
        QApplication.clipboard().setText(text)
        self.iface.messageBar().pushMessage("AutoContour", "Values copied.", level=MSG_INFO, duration=2)

    def clear_area(self):
        self.area = None
        self._clear_band()
        self._canvas().refresh()
        self._update_info()

    def use_view(self):
        self._set_area(self._to_wgs84(self._canvas().extent()))

    def start_draw(self):
        canvas = self._canvas()
        if self.draw_tool is None:
            self.draw_tool = QgsMapToolExtent(canvas)
            self.draw_tool.extentChanged.connect(self._drawn)
        self._prev_tool = canvas.mapTool()
        canvas.setMapTool(self.draw_tool)
        if self._esc is None:
            self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.iface.mainWindow())
            self._esc.activated.connect(self._cancel_draw)
        self._esc.setEnabled(True)
        canvas.setFocus()
        self.log("Draw a rectangle on the map (Esc to cancel).")
        self.showMinimized()

    def _end_draw(self):
        canvas = self._canvas()
        if self._esc is not None:
            self._esc.setEnabled(False)
        if self.probe_tool is not None and canvas.mapTool() is self.probe_tool:
            self._stop_probe()
            return
        if self.draw_tool is not None and canvas.mapTool() is self.draw_tool:
            if self._prev_tool is not None:
                canvas.setMapTool(self._prev_tool)
            else:
                canvas.unsetMapTool(self.draw_tool)
        self._prev_tool = None
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _drawn(self, rect):
        if rect.isEmpty() or rect.isNull():
            return
        self._set_area(self._to_wgs84(QgsRectangle(rect)))
        self._end_draw()

    def _cancel_draw(self):
        self._end_draw()

    def _show_band(self, rect_wgs84):
        canvas = self._canvas()
        self._clear_band()
        self.band = QgsRubberBand(canvas, GEOM_POLYGON)
        self.band.setColor(QColor(220, 60, 20, 255))
        self.band.setFillColor(QColor(220, 60, 20, 40))
        self.band.setWidth(2)
        dst = canvas.mapSettings().destinationCrs()
        rect = rect_wgs84
        if dst != QgsCoordinateReferenceSystem(WGS84):
            rect = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), dst,
                                          QgsProject.instance()).transformBoundingBox(rect_wgs84)
        self.band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def _clear_band(self):
        if self.band is not None:
            self.band.reset(GEOM_POLYGON)
            self._canvas().scene().removeItem(self.band)
            self.band = None

    def _source_changed(self):
        k = self._choice()
        self.local_files.setVisible(k == "local")
        self.layer_box.setVisible(k == "layer")
        self.url_edit.setVisible(k == "url")
        self.file_unit.setVisible(k in ("local", "layer", "url"))
        self._update_info()

    def _update_info(self):
        choice = self._choice()
        if choice not in DOWNLOADS:
            self.credit.setText("Your own data: nothing is downloaded. Draw an area to cut a part, or "
                                "leave it empty to use the whole raster.")
            if self.area is None:
                self.area_lbl.setText("No area drawn: the whole raster will be used.")
            else:
                w, s, e, n = self.area
                self.area_lbl.setText("Area: %.0f km² (the raster is cut to it)." % auto.area_km2(w, s, e, n))
            return
        if choice != "auto":
            self.credit.setText(demio.SOURCES[choice]["credit"])
        else:
            self.credit.setText("Tries Copernicus GLO-30 first, then SRTM, then GLO-90 for larger areas. "
                                "Data: Copernicus DEM © DLR / Airbus (EU and ESA) or SRTM (NASA/USGS).")
        if self.area is None:
            self.area_lbl.setText("No area yet. Draw a rectangle on the map.")
            return
        w, s, e, n = self.area
        km2 = auto.area_km2(w, s, e, n)
        keys = auto.candidates(choice, w, s, e, n)
        txt = "Area: %.0f km²  (%.4f° to %.4f° N, %.4f° to %.4f° E)." % (km2, s, n, w, e)
        if keys and "wcs" in demio.SOURCES[keys[0]]:
            txt += "\nWill ask %s from its web service." % demio.SOURCES[keys[0]]["label"].split(" (")[0]
        elif keys:
            tiles, cells, _lim, cell_m = auto.estimate(keys[0], w, s, e, n)
            txt += "\nWill use %s: %d tile(s)." % (demio.SOURCES[keys[0]]["label"], tiles)
            if cells > auto.TARGET_CELLS:
                txt += "\nLarge area: the DEM is read at about %.0f m so it stays workable." % cell_m
            if choice not in ("auto", keys[0]):
                txt += "\n%s is too big for the area, so the coarser GLO-90 is used." % (
                    demio.SOURCES[choice]["label"].split(" (")[0])
        else:
            txt += ("\nThis area is too large to process. Draw a smaller area.")
        self.area_lbl.setText(txt)

    # ---------------------------------------------------------------- run
    def run(self):
        if self.task is not None:
            return
        kind = self._choice()
        local = kind not in DOWNLOADS
        if self.area is None and not local:
            QMessageBox.information(self, "AutoContour", "Draw an area on the map first.")
            return
        if not (self.contours.isChecked() or self.bands.isChecked() or self.spots.isChecked()
                or self.slope.isChecked() or self.aspect.isChecked()):
            QMessageBox.information(self, "AutoContour", "Tick at least one output: contours, polygons or spot elevations.")
            return
        spec = None
        if kind == "local":
            spec = [p for p in self.local_files.splitFilePaths(self.local_files.filePath()) if p]
            if not spec:
                QMessageBox.information(self, "AutoContour", "Choose your DEM file(s) first.")
                return
        elif kind == "layer":
            lyr = self.layer_box.currentLayer()
            if lyr is None:
                QMessageBox.information(self, "AutoContour", "There is no raster layer in the project.")
                return
            spec = lyr.source().split("|")[0]
        elif kind == "url":
            spec = self.url_edit.text().strip()
            if not spec:
                QMessageBox.information(self, "AutoContour", "Paste the https link of the GeoTIFF.")
                return
        folder = self.folder.filePath().strip()
        if not folder:
            QMessageBox.warning(self, "AutoContour", "Choose a folder to save the results.")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "AutoContour", "Cannot use this folder: %s" % exc)
            return
        bbox = self.area
        if local:
            w = s = e = n = None
            keys = [kind]
        else:
            w, s, e, n = self.area
            keys = auto.candidates(kind, w, s, e, n)
        if not keys:
            QMessageBox.warning(self, "AutoContour", self.area_lbl.text().split("\n")[-1])
            return
        qs = QgsSettings()
        qs.setValue(SETTINGS + "folder", folder)
        qs.setValue(SETTINGS + "fmt", self.fmt.currentIndex())
        stamp = layers.run_stamp()
        out_unit = self._unit()
        fmt = self._fmt()
        want_spots = self.spots.isChecked()
        hill_path = os.path.join(folder, "hillshade_%s.tif" % stamp) if self.hill.isChecked() else None
        slope_path = os.path.join(folder, "slope_%s.tif" % stamp) if self.slope.isChecked() else None
        aspect_path = os.path.join(folder, "aspect_%s.tif" % stamp) if self.aspect.isChecked() else None
        chosen = self.interval.currentText().strip().replace(",", ".")
        try:
            user_interval = None if chosen.lower() in ("automatic", "") else float(chosen)
        except ValueError:
            QMessageBox.warning(self, "AutoContour", "The interval must be a number, for example 100 or 0.5.")
            return
        if user_interval is not None and user_interval <= 0:
            QMessageBox.warning(self, "AutoContour", "The interval must be larger than 0.")
            return
        want_contours = self.contours.isChecked()
        want_bands = self.bands.isChecked()
        want_lows = self.lows.isChecked()
        file_unit = "ft" if self.file_unit.currentIndex() == 1 else "m"
        explicit = kind != "auto" and keys[0] == kind

        def work(task):
            dem_path, used, last_err = None, None, None
            if local:
                dem_path = os.path.join(folder, "dem_local_%s.tif" % stamp)
                task.message.emit("Reading your DEM ...")
                task.setProgress(2)
                demio.prepare_local(kind, spec, bbox, dem_path, log=task.message.emit)
                used = kind
                keys_to_try = []
            else:
                keys_to_try = keys
            for key in keys_to_try:
                if task.isCanceled():
                    return None
                path = os.path.join(folder, "dem_%s_%s.tif" % (key, stamp))
                task.message.emit("Downloading %s ..." % demio.SOURCES[key]["label"])
                try:
                    task.setProgress(2)
                    if demio.download_tiles(key, w, s, e, n, path, is_canceled=task.isCanceled,
                                            log=task.message.emit) is None:
                        return None
                    dem_path, used = path, key
                    break
                except RuntimeError as exc:
                    last_err = str(exc)
                    task.message.emit("%s failed: %s" % (demio.SOURCES[key]["label"], last_err))
                    if explicit:
                        break
            if dem_path is None:
                raise RuntimeError(last_err or "No DEM could be downloaded.")
            d = demio.read_dem(dem_path)
            dem_unit = file_unit if local else "m"
            z_out = dem.to_unit(d["z"], dem_unit, out_unit)
            if not np.isfinite(z_out).any():
                raise RuntimeError("The DEM has no elevation values in this area (open sea?).")
            zmin, zmax = float(np.nanmin(z_out)), float(np.nanmax(z_out))
            interval = user_interval or auto.nice_interval(zmax - zmin, out_unit)
            cell_m = float(d["dy"]) * 111320.0 if d.get("geographic") else float(d["dy"])
            radius, relief = auto.spot_settings(cell_m, interval)
            params = {
                "dem_unit": dem_unit, "out_unit": out_unit, "contours": want_contours,
                "bands": want_bands,
                "sea_out": self.sea_out.isChecked() and kind not in ("emodnet", "etopo"),
                "interval": interval, "base": 0.0, "index_n": 5, "smooth": 1, "min_points": 8,
                "spots": want_spots, "peaks": True, "lows": want_lows,
                "radius": radius, "relief": relief, "round": True,
                "hillshade_path": hill_path, "slope_path": slope_path, "aspect_path": aspect_path, "geographic": bool(d.get("geographic")),
            }
            if interval < 1.0 and out_unit == "m":
                task.message.emit("Note: below 1 m the lines follow the noise of the DEM unless it is very "
                                  "precise (drone, LiDAR or survey). Public DEMs are good to about 1 m at best.")
            task.message.emit("DEM ready (%s). Interval: %g %s. Computing..." % (
                os.path.basename(dem_path), interval, out_unit))
            res = pipeline.process(d, params, is_canceled=task.isCanceled, progress=task.setProgress)
            return d, res, params, dem_path, used

        def done(task, ok):
            self._finish()
            if task.error:
                self.log("Error: %s" % task.error)
                QMessageBox.warning(self, "AutoContour", self._friendly(task.error))
                return
            if not ok or task.result is None:
                self.log("Cancelled.")
                return
            d, res, params, dem_path, used = task.result
            try:
                self._build_outputs(d, res, params, dem_path, used, folder, fmt, stamp, out_unit)
                self.clear_area()
            except Exception as exc:  # shown to the user
                self.log("Error: %s" % exc)
                QMessageBox.warning(self, "AutoContour", str(exc))

        t = FuncTask("AutoContour", work, done)
        t.message.connect(self.log)
        self.task = t
        self._busy(True)
        self.bar.setValue(0)
        t.progressChanged.connect(self._on_progress)
        QgsApplication.taskManager().addTask(t)
        self.log("Working... a large area can take a few minutes.")

    @staticmethod
    def _friendly(msg):
        if "network" in msg.lower() or "could not read any dem tile" in msg.lower():
            return (msg + "\n\nThe download needs internet access to the Copernicus / SRTM servers "
                    "(AWS). Check your connection or firewall, then try again.")
        return msg

    def _build_outputs(self, d, res, params, dem_path, used, folder, fmt, stamp, unit):
        crs = QgsCoordinateReferenceSystem(WGS84)
        if d.get("wkt"):
            c2 = QgsCoordinateReferenceSystem()
            c2.createFromWkt(d["wkt"])
            if c2.isValid():
                crs = c2
        self._out_crs = crs
        name = demio.SOURCES[used]["label"] if used in demio.SOURCES else "Your own DEM"
        added = []
        rl = QgsRasterLayer(dem_path, "DEM: " + name)
        if rl.isValid():
            QgsProject.instance().addMapLayer(rl)
        if res.get("hillshade"):
            hl = QgsRasterLayer(res["hillshade"], "Hillshade")
            if hl.isValid():
                QgsProject.instance().addMapLayer(hl)
        self.last_dem = (dem_path, params["dem_unit"], name)
        self.last_view = (dem_path, params["dem_unit"], unit, res, params["interval"],
                          bool(params.get("sea_out")), DEFAULT_PALETTE)
        self.view3d_btn.setEnabled(True)
        self.probe_rl = None
        for key, title, entries in (
                ("slope", "Slope classes", [(k, lab, col) for k, (_a, _b, lab, col)
                                            in enumerate(dem.SLOPE_CLASSES, 1)]),
                ("aspect", "Aspect", list(((v, lab, col) for v, lab, col in dem.ASPECT_CLASSES)))):
            if res.get(key):
                cl = QgsRasterLayer(res[key], title)
                if cl.isValid():
                    QgsProject.instance().addMapLayer(cl)
                    style_classes(cl, entries)
                    added.append(title.lower())
        if params.get("bands") and res.get("bands"):
            bl = layers.new_memory_layer(
                "MultiPolygonZ", crs, "elevation_bands",
                [("elev_min", "dbl"), ("elev_max", "dbl"), ("label", "str"), ("elev_unit", "str")])
            rows = []
            for b in res["bands"]:
                g = QgsGeometry()
                g.fromWkb(b["wkb"])
                g.convertToMultiType()
                g.get().addZValue(float(b["lo"]))        # Z = lower elevation of the band
                rows.append((g, [b["lo"], b["hi"], "%g - %g %s" % (b["lo"], b["hi"], unit), unit]))
            layers.add_features(bl, rows)
            uri = layers.save_layer(bl, folder, "elevation_bands", fmt, stamp)
            vl = layers.load_layer(uri, "Elevation bands (%s, every %g)" % (unit, params["interval"]))
            style_bands(vl, DEFAULT_PALETTE)
            added.append("%d elevation polygons" % len(rows))
        if params.get("contours"):
            self._add_contours(res, params, crs, folder, fmt, stamp, unit, added)
        if params["spots"]:
            self._add_spots(res, crs, folder, fmt, stamp, unit, added)
        self._finish_outputs(name, d, res, params, folder, stamp, unit, added)

    def _add_contours(self, res, params, crs, folder, fmt, stamp, unit, added):
        out = layers.new_memory_layer(
            "MultiLineStringZ", crs, "contours",
            [("elev", "dbl"), ("elev_unit", "str"), ("is_index", "int")])
        rows = []
        for c in res["contours"]:
            lv = float(c["level"])
            g = QgsGeometry.fromPolyline([QgsPoint(x, y, lv) for x, y in c["coords"]])   # Z = elevation
            g.convertToMultiType()
            rows.append((g, [float(c["level"]), unit, c["is_index"]]))
        layers.add_features(out, rows)
        uri = layers.save_layer(out, folder, "contours", fmt, stamp)
        vl = layers.load_layer(uri, "Contours (%s, every %g)" % (unit, params["interval"]))
        style_contours(vl, unit)
        added.append("%d contours" % len(rows))

    def _add_spots(self, res, crs, folder, fmt, stamp, unit, added):
        out = layers.new_memory_layer(
            "PointZ", crs, "spot_elevations",
            [("elev", "dbl"), ("type", "str"), ("elev_unit", "str"),
             ("is_highest", "int"), ("label", "str")])
        rows = []
        for sp in res["spots"]:
            g = QgsGeometry(QgsPoint(sp["x"], sp["y"], float(sp["elev"])))        # Z = elevation
            rows.append((g, [sp["elev"], sp["type"], unit, sp["is_highest"], "%d" % sp["elev"]]))
        layers.add_features(out, rows)
        uri = layers.save_layer(out, folder, "spot_elevations", fmt, stamp)
        vl = layers.load_layer(uri, "Spot elevations (%s)" % unit)
        style_spots(vl)
        peaks = sum(1 for sp in res["spots"] if sp["type"] == "peak")
        added.append("%d peaks, %d low points" % (peaks, len(rows) - peaks))

    def _finish_outputs(self, name, d, res, params, folder, stamp, unit, added):
        text = pipeline.report_text(name, d, params, res, unit)
        rpath = os.path.join(folder, "autocontour_report_%s.txt" % stamp)
        with open(rpath, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        self.log(text)
        if self.kml.isChecked():
            self._write_kmz(res, folder, stamp, unit)
        self.log("Done: %s. Report saved: %s" % (", ".join(added), rpath))
        self.iface.mapCanvas().refresh()

    def _write_kmz(self, res, folder, stamp, unit):
        from . import kml
        try:
            path = os.path.join(folder, "autocontour_%s.kmz" % stamp)
            what = kml.write_kmz(path, res, self._out_crs, unit, DEFAULT_PALETTE,
                                 self.kml_exag.value())
            self.log("Google Earth file: %s (%s). Open it in Google Earth to see it in 3D." % (path, what))
        except Exception as exc:  # the QGIS layers are already saved, so only report this one
            self.log("Google Earth file not written: %s" % exc)

    def open_3d(self):
        # The viewer has no parent, like this window, so minimising QGIS never hides it.
        if not self.last_view:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from . import view3d
            if self.viewer is not None:
                with contextlib.suppress(Exception):
                    self.viewer.close()
            self.viewer = view3d.open_viewer(None, *self.last_view)
        except Exception as exc:  # show the reason instead of failing silently
            QMessageBox.warning(self, "AutoContour", "Cannot open the 3D view: %s" % exc)
        finally:
            QApplication.restoreOverrideCursor()

    # -------------------------------------------------------------- tasks
    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.log("Cancelling...")

    def _finish(self):
        self.task = None
        self._busy(False)
        self.bar.setValue(100)

    # -------------------------------------------------------------- reset
    def closeEvent(self, event):
        self.reset_state()
        super().closeEvent(event)

    def reject(self):
        self.reset_state()
        super().reject()

    def reset_state(self):
        if self._resetting:
            return
        self._resetting = True
        try:
            task = self.task
            if task is not None:
                with contextlib.suppress(Exception):
                    task.cancel()
                with contextlib.suppress(Exception):
                    task.progressChanged.disconnect()
                self.task = None
            canvas = self._canvas()
            if self.draw_tool is not None and canvas.mapTool() is self.draw_tool:
                canvas.unsetMapTool(self.draw_tool)
            if self.probe_tool is not None and canvas.mapTool() is self.probe_tool:
                canvas.unsetMapTool(self.probe_tool)
            if self.probe_marker is not None:
                canvas.scene().removeItem(self.probe_marker)
                self.probe_marker = None
            self.probe_btn.setText("Elevation at click")
            if self._esc is not None:
                self._esc.setEnabled(False)
            self._clear_band()
            self.area = None
            self.bar.setValue(0)
            self.log_view.clear()
            self.hill.setChecked(False)
            self.slope.setChecked(False)
            self.aspect.setChecked(False)
            self._busy(False)
            self._update_info()
        except Exception as exc:  # reset must never raise on close
            self._reset_error = str(exc)
        finally:
            self._resetting = False
