"""Floating elevation card shown on the map canvas by the click tool."""
import math

from qgis.core import QgsPointXY
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

TEAL = QColor("#0b7285")
INK = QColor("#14181f")
MUTED = QColor("#6b7380")
ARROW = QColor("#e8590c")
CARD_W = 460
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def dms(value, is_lat, decimals=2):
    """Degrees, minutes, seconds with the hemisphere letter after the numbers."""
    letter = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    total = int(round(abs(value) * 3600 * 10 ** decimals))
    d, rest = divmod(total, 3600 * 10 ** decimals)
    m, s = divmod(rest, 60 * 10 ** decimals)
    return "%d°%02d'%0*.*f\"%s" % (d, m, 3 + decimals, decimals, s / 10 ** decimals, letter)


def aspect_word(aspect):
    return DIRS[int(((aspect + 22.5) % 360) // 45)]


def _font(widget, px, bold=False):
    f = QFont(widget.font())
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class Compass(QWidget):
    """Small compass: the arrow points the way the ground slopes down."""

    SIZE = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.aspect = None
        self.setFixedSize(self.SIZE, self.SIZE)

    def set_aspect(self, aspect):
        self.aspect = aspect
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.SIZE / 2.0, self.SIZE / 2.0)
        r = self.SIZE / 2.0 - 8
        p.setPen(QPen(QColor("#c9d1dc"), 2))
        p.setBrush(QColor("#f6f8fb"))
        p.drawEllipse(c, r, r)
        p.setFont(_font(self, 11, True))
        for txt, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            a = math.radians(ang)
            q = QPointF(c.x() + (r - 11) * math.sin(a), c.y() - (r - 11) * math.cos(a))
            p.setPen(QColor("#e03131") if txt == "N" else MUTED)
            p.drawText(QRectF(q.x() - 9, q.y() - 8, 18, 16), Qt.AlignmentFlag.AlignCenter, txt)
        if self.aspect is None:
            p.setPen(MUTED)
            p.setFont(_font(self, 12, True))
            p.drawText(QRectF(0, 0, self.SIZE, self.SIZE), Qt.AlignmentFlag.AlignCenter, "flat")
            return
        a = math.radians(self.aspect)
        tip = QPointF(c.x() + (r - 26) * math.sin(a), c.y() - (r - 26) * math.cos(a))
        back = QPointF(c.x() - 14 * math.sin(a), c.y() + 14 * math.cos(a))
        p.setPen(QPen(ARROW, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(back, tip)
        left = QPointF(tip.x() - 12 * math.sin(a - 0.45), tip.y() + 12 * math.cos(a - 0.45))
        right = QPointF(tip.x() - 12 * math.sin(a + 0.45), tip.y() + 12 * math.cos(a + 0.45))
        p.setBrush(ARROW)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([tip, left, right]))
        p.setBrush(INK)
        p.drawEllipse(c, 4, 4)


class Tile(QFrame):
    def __init__(self, caption, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)
        self.cap = QLabel(caption.upper())
        self.cap.setObjectName("cap")
        self.val = QLabel("-")
        self.val.setObjectName("tileval")
        self.sub = QLabel("")
        self.sub.setObjectName("tilesub")
        for w in (self.cap, self.val, self.sub):
            lay.addWidget(w)


class ElevationCard(QFrame):
    """Card placed next to the clicked point. Parent: the map canvas."""

    copy_requested = pyqtSignal()
    plugin_requested = pyqtSignal()
    user_closed = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.marker = None
        self.setObjectName("elevCard")
        self.setFixedWidth(CARD_W)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setStyleSheet(
            "#elevCard{background:#ffffff;border:1px solid #c5cdd9;border-radius:12px;}"
            "#elevCard QLabel{color:#14181f;background:transparent;font-size:12px;}"
            "#head{background:#0b7285;border-top-left-radius:11px;border-top-right-radius:11px;}"
            "#head QLabel{color:#ffffff;font-weight:bold;font-size:11px;}"
            "#chip{background:rgba(255,255,255,0.22);border-radius:9px;color:#ffffff;"
            "padding:1px 8px;font-size:11px;font-weight:bold;}"
            "#cap{color:#6b7380;font-size:10px;font-weight:bold;}"
            "#tile{background:#f4f6fa;border:1px solid #e3e7ee;border-radius:9px;}"
            "#tileval{font-size:16px;font-weight:bold;}"
            "#tilesub{color:#6b7380;font-size:10px;}"
            "#panel{background:#f9fafc;border:1px solid #e3e7ee;border-radius:9px;}"
            "#coord{font-family:Consolas,'DejaVu Sans Mono',monospace;font-size:13px;font-weight:bold;}"
            "#warn{background:#fff4e0;color:#8a5200;border-radius:8px;padding:4px 8px;font-size:11px;}"
            "#elevCard QPushButton{background:#f3f5f9;border:1px solid #cdd4df;border-radius:7px;"
            "padding:6px 12px;color:#14181f;font-weight:bold;font-size:13px;}"
            "#elevCard QPushButton:hover{background:#e7ebf2;}"
            "#elevCard QPushButton#primaryBtn{background:#1d6fe8;color:white;border:1px solid #1859bd;}"
            "#elevCard QPushButton#primaryBtn:hover{background:#1859bd;}"
            "#closeb{background:rgba(255,255,255,0.2);border:none;color:#ffffff;border-radius:11px;"
            "padding:0;font-size:15px;}"
            "#closeb:hover{background:rgba(255,255,255,0.38);}")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setObjectName("head")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 8, 10, 8)
        hl.addWidget(QLabel("ELEVATION"))
        hl.addStretch(1)
        self.src_chip = QLabel("")
        self.src_chip.setObjectName("chip")
        hl.addWidget(self.src_chip)
        close = QPushButton("×")
        close.setObjectName("closeb")
        close.setFixedSize(22, 22)
        close.clicked.connect(self._close_clicked)
        hl.addWidget(close)
        outer.addWidget(head)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 14)
        body.setSpacing(10)
        outer.addLayout(body)

        top = QHBoxLayout()
        top.setSpacing(12)
        left = QVBoxLayout()
        cap = QLabel("ELEVATION / DEPTH")
        cap.setObjectName("cap")
        left.addWidget(cap)
        self.big = QLabel("-")
        self.big.setFont(_font(self, 40, True))
        self.big.setStyleSheet("color:#0b7285;font-size:40px;font-weight:bold;")
        left.addWidget(self.big)
        self.sub = QLabel("")
        self.sub.setStyleSheet("font-size:14px;color:#374151;")
        self.sub.setWordWrap(True)
        left.addWidget(self.sub)
        left.addStretch(1)
        top.addLayout(left, 1)
        cbox = QVBoxLayout()
        self.compass = Compass()
        cbox.addWidget(self.compass, 0, Qt.AlignmentFlag.AlignHCenter)
        self.facing = QLabel("")
        self.facing.setStyleSheet("color:#6b7380;font-size:11px;")
        self.facing.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        cbox.addWidget(self.facing)
        top.addLayout(cbox)
        body.addLayout(top)

        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_slope = Tile("Slope")
        self.t_face = Tile("Faces")
        self.t_relief = Tile("Local relief")
        for t in (self.t_slope, self.t_face, self.t_relief):
            tiles.addWidget(t)
        body.addLayout(tiles)

        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(12, 8, 12, 8)
        pl.setSpacing(2)
        cap2 = QLabel("POSITION")
        cap2.setObjectName("cap")
        pl.addWidget(cap2)
        self.lat_lbl = QLabel("")
        self.lon_lbl = QLabel("")
        for w in (self.lat_lbl, self.lon_lbl):
            w.setObjectName("coord")
            pl.addWidget(w)
        self.dec_lbl = QLabel("")
        self.dec_lbl.setStyleSheet("color:#6b7380;font-size:11px;")
        pl.addWidget(self.dec_lbl)
        self.meta_lbl = QLabel("")
        self.meta_lbl.setStyleSheet("color:#6b7380;font-size:11px;")
        pl.addWidget(self.meta_lbl)
        body.addWidget(panel)

        self.warn = QLabel("")
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        body.addWidget(self.warn)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy values")
        self.copy_btn.setObjectName("primaryBtn")
        self.copy_btn.clicked.connect(self.copy_requested.emit)
        self.panel_btn = QPushButton("Settings && layers")
        self.panel_btn.clicked.connect(self.plugin_requested.emit)
        row.addWidget(self.copy_btn)
        row.addWidget(self.panel_btn)
        body.addLayout(row)
        self.hide()

    def show_info(self, data, map_point, pixel):
        """data keys: elev_m, elev_ft, slope, slope_pct, aspect, relief, radius_m, lat, lon,
        source, cell_m, note."""
        self.big.setText("%.1f m" % data["elev_m"] if data["elev_m"] is not None else "no data")
        if data["elev_m"] is not None:
            self.sub.setText("%.0f ft<br><b>%s</b>" % (data["elev_ft"], data.get("kind", "")))
        else:
            self.sub.setText("Outside the DEM or a void.")
        self.compass.set_aspect(data["aspect"] if data["slope"] is not None and data["slope"] >= 1.0 else None)
        sl = data["slope"]
        if sl is None:
            self.t_slope.val.setText("-")
            self.t_slope.sub.setText("")
            self.t_face.val.setText("-")
            self.t_face.sub.setText("")
            self.facing.setText("")
        else:
            self.t_slope.val.setText("%.1f°" % sl)
            self.t_slope.sub.setText("%.0f %% grade" % data["slope_pct"])
            if sl < 1.0:
                self.t_face.val.setText("Flat")
                self.t_face.sub.setText("under 1°")
                self.facing.setText("flat ground")
            else:
                w = aspect_word(data["aspect"])
                self.t_face.val.setText(w)
                self.t_face.sub.setText("%.0f° from north" % data["aspect"])
                self.facing.setText("the ground slopes down toward %s" % w)
        if data["relief"] is None:
            self.t_relief.val.setText("-")
            self.t_relief.sub.setText("")
        else:
            self.t_relief.val.setText("%.0f m" % data["relief"])
            self.t_relief.sub.setText("within %.0f m" % data["radius_m"])
        self.lat_lbl.setText("LAT   " + dms(data["lat"], True))
        self.lon_lbl.setText("LON  " + dms(data["lon"], False))
        self.dec_lbl.setText("%.6f°, %.6f°" % (data["lat"], data["lon"]))
        self.meta_lbl.setText("cell size about %.0f m" % data["cell_m"])
        self.src_chip.setText(data["source"])
        if data.get("note"):
            self.warn.setText(data["note"])
            self.warn.show()
        else:
            self.warn.hide()
        self._set_marker(map_point)
        self.adjustSize()
        self.move_near(pixel)
        self.show()
        self.raise_()

    def move_near(self, pixel):
        cw, ch = self.canvas.width(), self.canvas.height()
        w, h = self.width(), self.height()
        x, y = int(pixel.x()) + 18, int(pixel.y()) + 18
        if x + w > cw - 6:
            x = int(pixel.x()) - w - 18
        if y + h > ch - 6:
            y = int(pixel.y()) - h - 18
        self.move(max(6, min(x, cw - w - 6)), max(6, min(y, ch - h - 6)))

    def _set_marker(self, map_point):
        if self.marker is None:
            self.marker = QgsVertexMarker(self.canvas)
            icon = getattr(QgsVertexMarker, "ICON_CIRCLE", None)
            if icon is None:
                icon = QgsVertexMarker.IconType.ICON_CIRCLE
            self.marker.setIconType(icon)
            self.marker.setIconSize(14)
            self.marker.setPenWidth(3)
        self.marker.setColor(TEAL)
        self.marker.setCenter(QgsPointXY(map_point))

    def clear_marker(self):
        if self.marker is not None:
            self.canvas.scene().removeItem(self.marker)
            self.marker = None

    def _close_clicked(self):
        self.dismiss()
        self.user_closed.emit()

    def dismiss(self):
        self.clear_marker()
        self.hide()
