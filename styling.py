"""Default styles and labels."""
from qgis.core import (
    QgsPalettedRasterRenderer,
    QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsRendererCategory,
    QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsRuleBasedRenderer,
    QgsVectorLayerSimpleLabeling,
)


def _rules(root_symbol, items):
    root = QgsRuleBasedRenderer.Rule(None)
    for sym, expr, label in items:
        root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, label))
    return QgsRuleBasedRenderer(root)


def style_contours(layer, unit):
    normal = QgsLineSymbol.createSimple({"color": "#a0522d", "width": "0.18"})
    index = QgsLineSymbol.createSimple({"color": "#7b3f1d", "width": "0.45"})
    layer.setRenderer(_rules(None, [
        (index, '"is_index" = 1', "Index contour"),
        (normal, '"is_index" = 0', "Contour"),
    ]))
    s = QgsPalLayerSettings()
    s.fieldName = 'CASE WHEN "is_index" = 1 THEN to_string(round("elev")) END'
    s.isExpression = True
    s.enabled = True
    try:
        s.placement = QgsPalLayerSettings.Placement.Line
    except AttributeError:
        pass
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def style_spots(layer):
    peak = QgsMarkerSymbol.createSimple(
        {"name": "triangle", "color": "#222222", "size": "2.4"})
    low = QgsMarkerSymbol.createSimple(
        {"name": "circle", "color": "#ffffff", "outline_color": "#222222", "size": "1.8"})
    high = QgsMarkerSymbol.createSimple(
        {"name": "star", "color": "#c0392b", "size": "4"})
    layer.setRenderer(_rules(None, [
        (high, '"is_highest" = 1', "Highest elevation"),
        (peak, '"type" = \'peak\'', "Peak"),
        (low, '"type" = \'low\'', "Low"),
    ]))
    s = QgsPalLayerSettings()
    s.fieldName = "label"
    s.enabled = True
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


PALETTES = {
    "icao_a": ("ICAO tints: yellow-orange",
               ["#add15e", "#d8e375", "#eff1b6", "#faf8c2", "#f7f28d", "#fce82e", "#f6cd12",
                "#fcc10f", "#fad078", "#f9ce96", "#f3d8bd", "#fbe6db", "#ffffff"]),
}
DEFAULT_PALETTE = "icao_a"


def _rgb(name):
    cols = PALETTES.get(name, PALETTES[DEFAULT_PALETTE])[1]
    return [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in cols]


def tint_array(t, name=DEFAULT_PALETTE):
    """Continuous tint for t in 0..1 (numpy array) -> uint8 array (..., 3)."""
    import numpy as np
    pal = _rgb(name)
    t = np.clip(np.asarray(t, dtype=float), 0.0, 1.0)
    xs = np.linspace(0.0, 1.0, len(pal))
    out = np.empty(t.shape + (3,), dtype=np.uint8)
    for k in range(3):
        out[..., k] = np.round(np.interp(t, xs, [c[k] for c in pal])).astype(np.uint8)
    return out


def band_colours(n, name=DEFAULT_PALETTE):
    """One (r, g, b) per elevation band. Up to the number of tints of the chart the exact chart
    tints are used, spread from the lowest to the highest band; with more bands they are blended."""
    pal = _rgb(name)
    n = max(int(n), 1)
    if n <= len(pal):
        return [pal[int(round(i * (len(pal) - 1) / max(n - 1, 1)))] for i in range(n)]
    arr = tint_array([i / float(n - 1) for i in range(n)], name)
    return [tuple(int(v) for v in row) for row in arr]


def style_bands(layer, palette=DEFAULT_PALETTE):
    """One tint per elevation band, from the chosen hypsometric tint guide."""
    vals = sorted({(f["elev_min"], f["label"]) for f in layer.getFeatures()})
    colours = band_colours(len(vals), palette)
    cats = []
    for (_lo, label), rgb in zip(vals, colours):
        sym = QgsFillSymbol.createSimple({"color": "#%02x%02x%02x" % rgb, "outline_color": "#7a6a55",
                                          "outline_width": "0.1"})
        cats.append(QgsRendererCategory(label, sym, label))
    layer.setRenderer(QgsCategorizedSymbolRenderer("label", cats))
    layer.setOpacity(0.85)
    layer.triggerRepaint()


def style_classes(layer, entries):
    """entries: [(raster value, label, '#rrggbb')]."""
    from qgis.PyQt.QtGui import QColor
    classes = [QgsPalettedRasterRenderer.Class(v, QColor(c), lab) for v, lab, c in entries]
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))
    layer.setOpacity(0.8)
    layer.triggerRepaint()
