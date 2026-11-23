"""Built-in 3D viewer (numpy + Qt painter, no OpenGL needed, works on every QGIS 3.22+)."""
import math
import os

import numpy as np
from osgeo import gdal
from qgis.PyQt.QtCore import QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import dem
from .styling import DEFAULT_PALETTE, band_colours, tint_array

MAX_SIDE = 1000         # grid cells on the longest side
SEA_RGB = (120, 170, 215)
BG = (24, 28, 36)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def hypso(t, palette=DEFAULT_PALETTE):
    """t in 0..1 (array) -> uint8 (..., 3), from the chosen tint guide."""
    return tint_array(t, palette)


class Scene:
    """Heights, colours and overlays of one DEM, in metres."""

    def __init__(self, dem_path, dem_unit, unit, contours, spots, interval, sea_out):
        ds = gdal.Open(dem_path)
        if ds is None:
            raise ValueError("Cannot open the DEM for the 3D view.")
        w0, h0 = ds.RasterXSize, ds.RasterYSize
        k = max(w0, h0) / float(MAX_SIDE)
        w, h = max(int(w0 / max(k, 1.0)), 2), max(int(h0 / max(k, 1.0)), 2)
        small = gdal.Translate("", ds, format="MEM", width=w, height=h, resampleAlg="bilinear")
        band = small.GetRasterBand(1)
        z = band.ReadAsArray().astype(np.float64)
        nod = band.GetNoDataValue()
        bad = ~np.isfinite(z)
        if nod is not None:
            bad |= z == nod
        bad |= z < -1000
        if bad.all():
            raise ValueError("The DEM has no valid data.")
        z[bad] = np.nan
        self.gt = small.GetGeoTransform()
        self.h, self.w = z.shape
        geographic = "GEOGCS" in (small.GetProjection() or "") and "PROJCS" not in (small.GetProjection() or "")
        gt = self.gt
        lat = gt[3] + gt[5] * self.h / 2.0
        if geographic:
            self.dy = abs(gt[5]) * 111320.0
            self.dx = abs(gt[1]) * 111320.0 * max(math.cos(math.radians(lat)), 0.05)
        else:
            self.dx, self.dy = abs(gt[1]), abs(gt[5])
        zm = dem.to_unit(z, dem_unit, "m")
        self.sea = (zm <= 0.0) if sea_out else np.zeros_like(zm, dtype=bool)
        zm = np.where(self.sea, 0.0, zm)
        self.zm = np.where(np.isfinite(zm), zm, np.nanmin(zm))
        self.valid = np.isfinite(zm)
        self.unit = unit
        self.to_out = dem.to_unit(np.array([1.0]), "m", unit)[0]
        self.interval = float(interval)
        land = self.zm[~self.sea]
        self.zmin = float(land.min()) if land.size else float(self.zm.min())
        self.zmax = float(self.zm.max())
        self.width_m = (self.w - 1) * self.dx
        self.depth_m = (self.h - 1) * self.dy
        self.size_m = max(self.width_m, self.depth_m)
        self.relief = max(self.zmax - self.zmin, 1.0)
        # vertex grid (x east, y north, metres; origin = centre)
        cols = (np.arange(self.w) - (self.w - 1) / 2.0) * self.dx
        rows = ((self.h - 1) / 2.0 - np.arange(self.h)) * self.dy
        self.X, self.Y = np.meshgrid(cols, rows)
        self._colours = {}
        self._shades = {}
        self.clabels = []
        self.contours = self._prep_contours(contours or [])
        self.clabels = self.clabels[::max(1, len(self.clabels) // 40)]
        self.spots = self._prep_spots(spots or [])

    # ----------------------------------------------------------- geometry
    def _to_scene(self, x, y):
        gt = self.gt
        col = (np.asarray(x) - gt[0]) / gt[1] - 0.5
        row = (np.asarray(y) - gt[3]) / gt[5] - 0.5
        return col, row

    def _height_at(self, col, row):
        c = np.clip(col, 0, self.w - 1.001)
        r = np.clip(row, 0, self.h - 1.001)
        c0, r0 = c.astype(int), r.astype(int)
        fc, fr = c - c0, r - r0
        z = self.zm
        top = z[r0, c0] * (1 - fc) + z[r0, c0 + 1] * fc
        bot = z[r0 + 1, c0] * (1 - fc) + z[r0 + 1, c0 + 1] * fc
        return top * (1 - fr) + bot * fr

    def _xyz(self, x, y):
        col, row = self._to_scene(x, y)
        xs = (col - (self.w - 1) / 2.0) * self.dx
        ys = ((self.h - 1) / 2.0 - row) * self.dy
        return xs, ys, self._height_at(col, row), col, row

    def _prep_contours(self, contours):
        arrs, lid = [], []
        total = sum(len(c["coords"]) for c in contours)
        skip = max(1, total // 250000)
        keep = []
        for k, c in enumerate(contours):
            a = np.asarray(c["coords"], dtype=float)[::skip]
            if len(a) < 2:
                continue
            arrs.append(a)
            lid.append(np.full(len(a), len(keep)))
            keep.append(c)
        if not arrs:
            return None
        A, L = np.concatenate(arrs), np.concatenate(lid)
        xs, ys, zz, col, row = self._xyz(A[:, 0], A[:, 1])
        is_idx = np.array([int(c.get("is_index", 0)) for c in keep], dtype=np.uint8)
        levels = np.array([float(c["level"]) for c in keep])
        # labels: one point in the middle of some index contours
        starts = np.searchsorted(L, np.arange(len(keep)))
        ends = np.append(starts[1:], len(L))
        pick = np.nonzero(is_idx == 1)[0]
        pick = pick[::max(1, len(pick) // 40)]
        for k in pick:
            m = (starts[k] + ends[k]) // 2
            self.clabels.append((float(xs[m]), float(ys[m]), float(zz[m]), float(levels[k])))
        # densify every segment so the line follows the surface
        same = L[1:] == L[:-1]
        seg = np.hypot(np.diff(xs), np.diff(ys))
        step = max(self.dx, self.dy) * 0.3
        n = np.where(same, np.maximum(np.ceil(seg / step).astype(int), 1), 0)
        cap = 700000
        if n.sum() > cap:
            n = np.where(same, np.maximum((n * (cap / float(n.sum()))).astype(int), 1), 0)
        idx = np.repeat(np.arange(len(n)), n)
        offs = np.cumsum(n) - n
        t = (np.arange(len(idx)) - np.repeat(offs, n) + 1) / np.repeat(np.maximum(n, 1), n)
        cc = col[idx] + (col[idx + 1] - col[idx]) * t
        rr = row[idx] + (row[idx + 1] - row[idx]) * t
        px = xs[idx] + (xs[idx + 1] - xs[idx]) * t
        py = ys[idx] + (ys[idx + 1] - ys[idx]) * t
        pz = self._height_at(cc, rr)
        return np.stack([px, py, pz], axis=1), is_idx[L[idx]]

    def _prep_spots(self, spots):
        out = []
        for s in spots:
            xs, ys, zz, col, row = self._xyz(s["x"], s["y"])
            if 0 <= col <= self.w - 1 and 0 <= row <= self.h - 1:
                out.append((float(xs), float(ys), float(zz), s["type"], int(s.get("is_highest", 0)),
                            float(s["elev"])))
        return out

    # ------------------------------------------------------------ colours
    def shade(self, zscale):
        key = round(zscale, 2)
        if key not in self._shades:
            self._shades[key] = dem.hillshade(self.zm * zscale, self.dx, self.dy).astype(float) / 255.0
        return self._shades[key]

    def colours(self, mode, zscale, palette=DEFAULT_PALETTE):
        key = (mode, round(zscale, 2) if mode == "hill" else 0, palette)
        if key in self._colours:
            return self._colours[key]
        z = self.zm
        span = max(self.zmax - self.zmin, 1e-6)
        if mode == "bands":
            iv = self.interval / (self.to_out or 1.0)
            idx = np.floor(z / iv)
            lo, hi = np.floor(self.zmin / iv), np.floor(self.zmax / iv)
            nb = int(hi - lo) + 1
            table = np.array(band_colours(nb, palette), dtype=np.uint8)
            rgb = table[np.clip(idx - lo, 0, nb - 1).astype(int)]
        elif mode == "hill":
            hs = self.shade(zscale)
            rgb = (hypso((z - self.zmin) / span, palette).astype(float) * (0.35 + 0.75 * hs[..., None])).clip(0, 255).astype(np.uint8)
        elif mode == "slope":
            slope, _a = dem.slope_aspect(z, self.dx, self.dy)
            cls = dem.slope_classes(slope)
            pal = np.array([(90, 90, 90)] + [_hex(c[3]) for c in dem.SLOPE_CLASSES], dtype=np.uint8)
            rgb = pal[cls]
        elif mode == "aspect":
            slope, aspect = dem.slope_aspect(z, self.dx, self.dy)
            cls = dem.aspect_classes(aspect, slope)
            pal = np.array([(90, 90, 90)] + [_hex(c[2]) for c in dem.ASPECT_CLASSES], dtype=np.uint8)
            rgb = pal[cls]
        else:                                   # colours
            rgb = hypso((z - self.zmin) / span, palette)
        rgb = rgb.copy()
        rgb[self.sea] = SEA_RGB
        shade = self.shade(zscale)
        if mode != "hill":                      # light relief shading under every surface
            rgb = (rgb.astype(float) * (0.62 + 0.45 * shade[..., None])).clip(0, 255).astype(np.uint8)
        self._colours[key] = rgb.reshape(-1, 3)
        return self._colours[key]


class Camera:
    def __init__(self, scene, zscale):
        self.scene = scene
        self.zscale = zscale
        self.reset()

    def reset(self):
        self.yaw, self.pitch = 205.0, 38.0
        self.dist = 1.9 * self.scene.size_m
        self.cz = (self.scene.zmin + self.scene.zmax) / 2.0 * self.zscale

    def basis(self):
        yaw, pit = math.radians(self.yaw), math.radians(self.pitch)
        eye = np.array([math.sin(yaw) * math.cos(pit), math.cos(yaw) * math.cos(pit), math.sin(pit)])
        eye = eye * self.dist + np.array([0.0, 0.0, self.cz])
        fwd = np.array([0.0, 0.0, self.cz]) - eye
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        return eye, right, up, fwd

    def project(self, p, w, h):
        """p: (n,3) scene metres (z already scaled) -> sx, sy, depth, visible."""
        eye, right, up, fwd = self.basis()
        d = p - eye
        xc, yc, zc = d @ right, d @ up, d @ fwd
        f = 0.5 * h / math.tan(math.radians(17.5))
        vis = zc > self.dist * 0.05
        zc_safe = np.where(vis, zc, 1.0)
        sx = w / 2.0 + f * xc / zc_safe
        sy = h / 2.0 - f * yc / zc_safe
        return sx, sy, zc, vis, f


THEMES = {"dark": ((24, 28, 36), (255, 255, 255), (0, 0, 0)),
          "light": ((226, 233, 241), (30, 34, 40), (255, 255, 255)),
          "white": ((255, 255, 255), (30, 34, 40), (255, 255, 255))}

DEFAULTS = {"palette": DEFAULT_PALETTE, "shape": "smooth", "mode": "bands", "surface": True, "contours": "all", "clabels": True, "peaks": True,
            "lows": True, "highest": True, "spot_labels": True, "sea": True, "wire": False,
            "block": False, "north": True, "legend": True, "theme": "dark"}


def _spot_visible(s, o):
    kind, hi = s[3], s[4]
    if hi:
        return o["highest"]
    return o["peaks"] if kind == "peak" else o["lows"]


def _cell_mean(a):
    return (a[:-1, :-1] + a[1:, :-1] + a[:-1, 1:] + a[1:, 1:]) / 4.0


def _indices(scene, cam, w, h, target_px, cap):
    """Rows and columns of the grid to draw: only the part that can be seen, at a step that
    gives cells of about `target_px` pixels, and never more than `cap` cells."""
    f = 0.5 * h / math.tan(math.radians(17.5))
    cell_px = f * max(scene.dx, scene.dy) / max(cam.dist, 1.0)
    k = int(min(max(math.ceil(target_px / max(cell_px, 1e-6)), 1), max(scene.h, scene.w) // 8 + 1))
    r0, r1, c0, c1 = 0, scene.h - 1, 0, scene.w - 1
    if cell_px * 3 > 8:                         # zoomed in: find the part inside the window
        q = 6
        ci = np.arange(0, scene.h, q)
        cj = np.arange(0, scene.w, q)
        P = np.stack([scene.X[np.ix_(ci, cj)].ravel(), scene.Y[np.ix_(ci, cj)].ravel(),
                      scene.zm[np.ix_(ci, cj)].ravel() * cam.zscale], axis=1)
        sx, sy, _d, vis, _f = cam.project(P, w, h)
        m = (vis & (sx > -40) & (sx < w + 40) & (sy > -40) & (sy < h + 40)).reshape(len(ci), len(cj))
        if m.any():
            rows, cols = np.nonzero(m.any(axis=1))[0], np.nonzero(m.any(axis=0))[0]
            r0, r1 = max(int(ci[rows[0]]) - 2 * q, 0), min(int(ci[rows[-1]]) + 2 * q, scene.h - 1)
            c0, c1 = max(int(cj[cols[0]]) - 2 * q, 0), min(int(cj[cols[-1]]) + 2 * q, scene.w - 1)
    nr, nc = (r1 - r0) / float(k), (c1 - c0) / float(k)
    if nr * nc > cap:
        k = int(math.ceil(k * math.sqrt(nr * nc / float(cap))))
    ii = np.unique(np.append(np.arange(r0, r1 + 1, k), r1))
    jj = np.unique(np.append(np.arange(c0, c1 + 1, k), c1))
    return ii, jj, k


def _bilin(a, ri, rj):
    r0 = np.clip(np.floor(ri).astype(int), 0, a.shape[0] - 2)
    c0 = np.clip(np.floor(rj).astype(int), 0, a.shape[1] - 2)
    fr = (ri - r0)[:, None]
    fc = (rj - c0)[None, :]
    a00, a01 = a[np.ix_(r0, c0)], a[np.ix_(r0, c0 + 1)]
    a10, a11 = a[np.ix_(r0 + 1, c0)], a[np.ix_(r0 + 1, c0 + 1)]
    return (a00 * (1 - fc) + a01 * fc) * (1 - fr) + (a10 * (1 - fc) + a11 * fc) * fr


def _verts(scene, o, zs, ri, rj, Cfull, refined):
    """Heights, colours and masks on the grid lines ri x rj (fractional when refined)."""
    if not refined:
        sub = np.ix_(ri.astype(int), rj.astype(int))
        return (scene.X[sub], scene.Y[sub], scene.zm[sub], Cfull[sub].astype(float),
                scene.sea[sub], scene.valid[sub])
    Z = _bilin(scene.zm, ri, rj)
    near = np.ix_(np.rint(ri).astype(int), np.rint(rj).astype(int))
    sea_v, ok_v = scene.sea[near], scene.valid[near]
    if o["mode"] == "bands":
        # recolour from the refined heights so the band edges are sharp, not blocky
        iv = scene.interval / (scene.to_out or 1.0)
        lo, hi = int(np.floor(scene.zmin / iv)), int(np.floor(scene.zmax / iv))
        nb = hi - lo + 1
        table = np.array(band_colours(nb, o["palette"]), dtype=float)
        C = table[np.clip(np.floor(Z / iv).astype(int) - lo, 0, nb - 1)]
        C[sea_v] = SEA_RGB
        C = C * (0.62 + 0.45 * _bilin(scene.shade(zs), ri, rj))[..., None]
    else:
        C = np.stack([_bilin(Cfull[..., ch].astype(float), ri, rj) for ch in range(3)], axis=-1)
    X = np.broadcast_to(((rj - (scene.w - 1) / 2.0) * scene.dx)[None, :], (len(ri), len(rj)))
    Y = np.broadcast_to((((scene.h - 1) / 2.0 - ri) * scene.dy)[:, None], (len(ri), len(rj)))
    return X, Y, Z, C, sea_v, ok_v


def _lod_quads(scene, cam, W, H, o, zs):
    """Smooth terrain with detail where the camera is close and coarser cells far away."""
    f = 0.5 * H / math.tan(math.radians(17.5))
    T = 16
    Cfull = scene.colours(o["mode"], zs, o["palette"]).reshape(scene.h, scene.w, 3)
    re = np.unique(np.append(np.arange(0, scene.h - 1, T), scene.h - 1))
    ce = np.unique(np.append(np.arange(0, scene.w - 1, T), scene.w - 1))
    sub = np.ix_(re, ce)
    P = np.stack([scene.X[sub], scene.Y[sub], scene.zm[sub] * zs], axis=-1).reshape(-1, 3)
    sx, sy, zc, vis, _f = cam.project(P, W, H)
    shape = (len(re), len(ce))
    sx, sy, zc, vis = sx.reshape(shape), sy.reshape(shape), zc.reshape(shape), vis.reshape(shape)

    def four(a):
        return np.stack([a[:-1, :-1], a[:-1, 1:], a[1:, :-1], a[1:, 1:]])
    X4, Y4, Z4, V4 = four(sx), four(sy), four(zc), four(vis)
    seen = (V4.any(axis=0) & (X4.max(axis=0) > -30) & (X4.min(axis=0) < W + 30)
            & (Y4.max(axis=0) > -30) & (Y4.min(axis=0) < H + 30))
    depth = np.maximum(Z4.mean(axis=0), 1.0)
    cell_px = f * max(scene.dx, scene.dy) / depth
    quads, cols = [], []
    blocks = list(zip(*np.nonzero(seen)))
    pxs = np.array([float(cell_px[b]) for b in blocks]) if blocks else np.zeros(0)
    size_b = np.array([float(max(re[b[0] + 1] - re[b[0]], ce[b[1] + 1] - ce[b[1]])) for b in blocks]) \
        if blocks else np.zeros(0)
    max_ref, mul = 6, 1
    while True:                                  # keep the number of drawn quads within a time budget
        refine = np.where(pxs > 4.5, np.minimum(np.ceil(pxs / 4.5), max_ref), 1).astype(int)
        step = np.where(pxs < 1.6, np.clip(np.floor(3.2 / np.maximum(pxs, 1e-6)), 1, T), 1).astype(int) * mul
        step = np.minimum(step, T)
        total = float(((size_b / step) * refine) ** 2 @ np.ones(len(size_b))) if len(size_b) else 0.0
        if total <= 42000 or (max_ref == 1 and mul >= T):
            break
        if max_ref > 1:
            max_ref -= 1
        else:
            mul += 1
    for n_b, (bi, bj) in enumerate(blocks):
        step_b, refine_b = int(step[n_b]), int(refine[n_b])
        ra, rb, ca, cb = re[bi], re[bi + 1], ce[bj], ce[bj + 1]
        if refine_b > 1:
            ri = np.linspace(ra, rb, (rb - ra) * refine_b + 1)
            rj = np.linspace(ca, cb, (cb - ca) * refine_b + 1)
        else:
            ri = np.unique(np.append(np.arange(ra, rb, step_b), rb)).astype(float)
            rj = np.unique(np.append(np.arange(ca, cb, step_b), cb)).astype(float)
        X, Y, Z, C, sea_v, ok_v = _verts(scene, o, zs, ri, rj, Cfull, refine_b > 1)
        ok = _cell_mean(ok_v.astype(float)) > 0.99
        if not o["sea"]:
            ok &= ~(_cell_mean(sea_v.astype(float)) >= 0.5)
        if not ok.any():
            continue
        zz = Z * zs
        q = np.stack([np.stack([X[:-1, :-1], Y[:-1, :-1], zz[:-1, :-1]], -1),
                      np.stack([X[:-1, 1:], Y[:-1, 1:], zz[:-1, 1:]], -1),
                      np.stack([X[1:, 1:], Y[1:, 1:], zz[1:, 1:]], -1),
                      np.stack([X[1:, :-1], Y[1:, :-1], zz[1:, :-1]], -1)], axis=2)
        quads.append(q[ok])
        cols.append(_cell_mean(C)[ok])
    return quads, cols


def _mesh(scene, cam, w, h, o, fast, bg):
    """Solid terrain drawn as quads. Returns an (h*w, 4) uint8 array."""
    ss = 1 if fast else 2                      # draw at 2x and shrink: smooth edges, no stair-steps
    W, H = w * ss, h * ss
    zs = cam.zscale
    f = 0.5 * H / math.tan(math.radians(17.5))
    if (not fast) and o["shape"] == "smooth" and not o["block"] \
            and f * max(scene.dx, scene.dy) / max(cam.dist, 1.0) > 2.2:
        quads, cols = _lod_quads(scene, cam, W, H, o, zs)
        return _paint(quads, cols, cam, w, h, ss, bg)
    ii, jj, k = _indices(scene, cam, w, h, 7.0 if fast else 3.2, 2600 if fast else 20000)
    ii, jj, k = _indices(scene, cam, w, h, 7.0 if fast else 3.2, 2600 if fast else 20000)
    Cfull = scene.colours(o["mode"], zs, o["palette"]).reshape(scene.h, scene.w, 3)
    X, Y, Z, C, sea_v, ok_v = _verts(scene, o, zs, ii.astype(float), jj.astype(float), Cfull, False)
    cell_ok = _cell_mean(ok_v.astype(float)) > 0.99
    cell_sea = _cell_mean(sea_v.astype(float)) >= 0.5
    if not o["sea"]:
        cell_ok &= ~cell_sea
    col = _cell_mean(C)
    base = (scene.zmin - 0.10 * scene.relief) * zs
    quads, cols = [], []

    def add(q, c, m):
        if m.any():
            quads.append(q[m])
            cols.append(c[m])

    def corners(zt):
        return np.stack([np.stack([X[:-1, :-1], Y[:-1, :-1], zt[0]], -1),
                         np.stack([X[:-1, 1:], Y[:-1, 1:], zt[1]], -1),
                         np.stack([X[1:, 1:], Y[1:, 1:], zt[2]], -1),
                         np.stack([X[1:, :-1], Y[1:, :-1], zt[3]], -1)], axis=2)

    if o["shape"] == "steps":
        ivm = scene.interval / (scene.to_out or 1.0)
        zc = _cell_mean(Z)
        lvl = np.floor(zc / ivm + 1e-9)
        hh = np.where(cell_sea, 0.0, lvl * ivm)
        lo, hi = np.floor(scene.zmin / ivm), np.floor(scene.zmax / ivm)
        if o["mode"] in ("bands", "colours", "hill"):
            nb = int(hi - lo) + 1
            col = np.array(band_colours(nb, o["palette"]), dtype=float)[np.clip(lvl - lo, 0, nb - 1).astype(int)]
            col[cell_sea] = SEA_RGB
        hz = hh * zs
        flat_t = (hz, hz, hz, hz)
        q = corners(flat_t)
        add(q, col, cell_ok)
        # walls between neighbouring cells of different height
        ni, nj = hh.shape
        for axis in (0, 1):
            if axis == 1:                       # east-west neighbours, shared edge = vertical line
                a_, b_ = hz[:, :-1], hz[:, 1:]
                ca, cb = col[:, :-1], col[:, 1:]
                oa, ob = cell_ok[:, :-1], cell_ok[:, 1:]
                px, py = X[:-1, 1:-1], Y[:-1, 1:-1]
                qx, qy = X[1:, 1:-1], Y[1:, 1:-1]
            else:                               # north-south neighbours, shared edge = horizontal line
                a_, b_ = hz[:-1, :], hz[1:, :]
                ca, cb = col[:-1, :], col[1:, :]
                oa, ob = cell_ok[:-1, :], cell_ok[1:, :]
                px, py = X[1:-1, :-1], Y[1:-1, :-1]
                qx, qy = X[1:-1, 1:], Y[1:-1, 1:]
            top, bot = np.maximum(a_, b_), np.minimum(a_, b_)
            wc = np.where((a_ >= b_)[..., None], ca, cb) * 0.72
            m = (a_ != b_) & oa & ob
            wq = np.stack([np.stack([px, py, top], -1), np.stack([qx, qy, top], -1),
                           np.stack([qx, qy, bot], -1), np.stack([px, py, bot], -1)], axis=2)
            add(wq, wc, m)
    else:
        q = corners((Z[:-1, :-1] * zs, Z[:-1, 1:] * zs, Z[1:, 1:] * zs, Z[1:, :-1] * zs))
        add(q, col, cell_ok)
    if o["block"]:
        pairs = [((X[0, :-1], Y[0, :-1], X[0, 1:], Y[0, 1:]), (0, slice(None))),
                 ((X[-1, :-1], Y[-1, :-1], X[-1, 1:], Y[-1, 1:]), (-1, slice(None))),
                 ((X[:-1, 0], Y[:-1, 0], X[1:, 0], Y[1:, 0]), (slice(None), 0)),
                 ((X[:-1, -1], Y[:-1, -1], X[1:, -1], Y[1:, -1]), (slice(None), -1))]
        edges = (ii[0] == 0, ii[-1] == scene.h - 1, jj[0] == 0, jj[-1] == scene.w - 1)
        for (x0, y0, x1, y1), cell_idx in [pq for pq, e in zip(pairs, edges) if e]:
            ck = col[cell_idx]
            if o["shape"] == "steps":
                t0 = t1 = hz[cell_idx]
            else:
                zz = Z * zs
                if cell_idx[0] == 0:
                    t0, t1 = zz[0, :-1], zz[0, 1:]
                elif cell_idx[0] == -1:
                    t0, t1 = zz[-1, :-1], zz[-1, 1:]
                elif cell_idx[1] == 0:
                    t0, t1 = zz[:-1, 0], zz[1:, 0]
                else:
                    t0, t1 = zz[:-1, -1], zz[1:, -1]
            bz = np.full_like(t0, base)
            wq = np.stack([np.stack([x0, y0, t0], -1), np.stack([x1, y1, t1], -1),
                           np.stack([x1, y1, bz], -1), np.stack([x0, y0, bz], -1)], axis=1)
            quads.append(wq)
            cols.append(ck * 0.62)
    return _paint(quads, cols, cam, w, h, ss, bg)


def _paint(quads, cols, cam, w, h, ss, bg):
    """Painter's algorithm: far quads first. Returns an (h*w, 4) uint8 array."""
    W, H = w * ss, h * ss
    img = QImage(W, H, QImage.Format.Format_RGBA8888)
    img.fill(QColor(*bg))
    if quads:
        Q = np.concatenate(quads)
        Cc = np.concatenate(cols)
        n = len(Q)
        sx, sy, zc_, vis, _f = cam.project(Q.reshape(-1, 3), W, H)
        sx, sy = sx.reshape(n, 4), sy.reshape(n, 4)
        vis = vis.reshape(n, 4).all(axis=1)
        depth = zc_.reshape(n, 4).mean(axis=1)
        inside = ((sx.max(axis=1) >= -8) & (sx.min(axis=1) <= W + 8)
                  & (sy.max(axis=1) >= -8) & (sy.min(axis=1) <= H + 8))
        keep = np.nonzero(vis & inside)[0]
        keep = keep[np.argsort(-depth[keep], kind="stable")]
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        cc = np.clip(Cc[keep], 0, 255).astype(int).tolist()
        pts = np.stack([sx[keep], sy[keep]], axis=2).tolist()
        for c, quad in zip(cc, pts):
            colour = QColor(c[0], c[1], c[2])
            p.setPen(colour)
            p.setBrush(colour)
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in quad]))
        p.end()
    if ss > 1:
        img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.bits()
    ptr.setsize(img.sizeInBytes())
    return np.frombuffer(ptr, dtype=np.uint8).reshape(h * w, 4).copy()




def render(scene, cam, w, h, o, fast=False, dpr=1.0):
    bg, fg, halo = THEMES.get(o["theme"], THEMES["dark"])
    zs = cam.zscale
    ii, jj, stride = _indices(scene, cam, w, h, 6.0 if fast else 2.5, 6000 if fast else 60000)
    sub = np.ix_(ii, jj)
    X = scene.X[sub].ravel()
    Y = scene.Y[sub].ravel()
    zsrc = scene.zm[sub].ravel()
    if o["shape"] == "steps":
        ivm = scene.interval / (scene.to_out or 1.0)
        zsrc = np.where(scene.sea[sub].ravel(), 0.0, np.floor(zsrc / ivm + 1e-9) * ivm)
    Z = zsrc * zs
    ok_cell = scene.valid[sub].ravel()
    if not o["sea"]:
        ok_cell = ok_cell & ~scene.sea[sub].ravel()
    if not o["surface"]:
        ok_cell = np.zeros_like(ok_cell)
    P = np.stack([X, Y, Z], axis=1)[ok_cell]
    zbuf = np.full(h * w, np.inf)
    size = 2
    if len(P):
        sx, sy, depth, vis, f = cam.project(P, w, h)
        vis &= (sx > -20) & (sx < w + 20) & (sy > -20) & (sy < h + 20)
        sx, sy, depth = sx[vis], sy[vis], depth[vis]
        if not len(depth):
            sx = sy = depth = np.zeros(1)
            depth[:] = 1.0
        near = max(float(np.percentile(depth, 4)), 1.0)
        spacing = f * max(scene.dx, scene.dy) * stride / near
        size = int(min(max(math.ceil(spacing * 1.6) + 1, 2), 9))
        order = np.argsort(-depth)
        sx, sy, depth = sx[order], sy[order], depth[order]
        sxi, syi = np.floor(sx).astype(int), np.floor(sy).astype(int)
        for oy in range(size):
            for ox in range(size):
                x, y = sxi + ox - size // 2, syi + oy - size // 2
                m = (x >= 0) & (x < w) & (y >= 0) & (y < h)
                zbuf[y[m] * w + x[m]] = depth[m]
    if o["surface"]:
        flat = _mesh(scene, cam, w, h, o, fast, bg)
    else:
        flat = np.empty((h * w, 4), dtype=np.uint8)
        flat[:, :3] = bg
        flat[:, 3] = 255
    img = flat.reshape(h, w, 4)
    tol = cam.dist * 0.025 + size * scene.dx * 0.05

    def dots(pts, colour, kinds=None, offset=0.002, wide=True):
        if fast:
            pts = pts[::3]
            kinds = kinds[::3] if kinds is not None else None
        q = pts.copy()
        if o["shape"] == "steps" and kinds is not None:
            ivm = scene.interval / (scene.to_out or 1.0)
            q[:, 2] = np.round(q[:, 2] / ivm) * ivm
        q[:, 2] = q[:, 2] * zs + offset * scene.size_m
        cx, cy, cd, cv, _f = cam.project(q, w, h)
        xi, yi = np.floor(cx).astype(int), np.floor(cy).astype(int)
        m = cv & (xi >= 1) & (xi < w - 2) & (yi >= 1) & (yi < h - 2)
        xi, yi, cd = xi[m], yi[m], cd[m]
        kk = kinds[m] if kinds is not None else None
        for dx_, dy_ in ((0, 0), (1, 0), (0, 1), (1, 1)) if (wide and not fast) else ((0, 0),):
            idx = (yi + dy_) * w + (xi + dx_)
            ok = cd <= zbuf[idx] + tol
            if kk is None:
                flat[idx[ok], :3] = colour
            else:
                flat[idx[ok & (kk == 0)], :3] = colour[0]
                flat[idx[ok & (kk == 1)], :3] = colour[1]

    if o["wire"] and o["surface"]:
        n = max(scene.w // 28, 2)
        lines = []
        for i in range(0, scene.h, n):
            lines.append(np.stack([scene.X[i], scene.Y[i], scene.zm[i]], axis=1))
        for j in range(0, scene.w, n):
            lines.append(np.stack([scene.X[:, j], scene.Y[:, j], scene.zm[:, j]], axis=1))
        dots(np.concatenate(lines), (35, 35, 35), offset=0.0005, wide=False)
    if o["contours"] != "off" and scene.contours is not None:
        pts, kinds = scene.contours
        if o["contours"] == "index":
            keep = kinds == 1
            pts, kinds = pts[keep], kinds[keep]
        if len(pts):
            dots(pts, ((96, 52, 22), (46, 22, 8)), kinds)
    qimg = QImage(img.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    out = qimg.copy()
    U = max(float(dpr), 1.0)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(U, U)                                  # sizes of text and symbols stay readable on high-DPI screens
    font = QFont()
    font.setPointSize(9)
    font.setBold(True)
    p.setFont(font)

    def label(x, y, text, ox=0.0, oy=0.0):
        x, y = x / U + ox, y / U + oy
        p.setPen(QColor(*halo))
        p.drawText(QPointF(x + 1, y + 1), text)
        p.setPen(QColor(*fg))
        p.drawText(QPointF(x, y), text)

    def visible(x, y, d, v):
        if not v or not (0 <= x < w and 0 <= y < h):
            return False
        return d <= zbuf[int(y) * w + int(x)] + cam.dist * 0.03

    if o["contours"] != "off" and o["clabels"] and scene.clabels:
        sp = np.array([[a, b, c] for a, b, c, _l in scene.clabels])
        sp[:, 2] = sp[:, 2] * zs + 0.004 * scene.size_m
        px, py, pd, pv, _f = cam.project(sp, w, h)
        for (_a, _b, _c, lv), x, y, d, v in zip(scene.clabels, px, py, pd, pv):
            if visible(x, y, d, v):
                label(x, y, "%g" % round(lv, 1), 3, -3)
    shown = [s for s in scene.spots if _spot_visible(s, o)]
    if shown:
        sp = np.array([[s[0], s[1], s[2] * zs] for s in shown])
        sp[:, 2] += 0.004 * scene.size_m
        px, py, pd, pv, _f = cam.project(sp, w, h)
        for (s, x, y, d, v) in zip(shown, px, py, pd, pv):
            if not visible(x, y, d, v):
                continue
            kind, hi = s[3], s[4]
            r = 7 if hi else 5
            x, y = x / U, y / U
            poly = QPolygonF()
            if kind == "peak":
                poly.append(QPointF(x, y - r - 2))
                poly.append(QPointF(x - r, y + r - 2))
                poly.append(QPointF(x + r, y + r - 2))
            else:
                for k in range(12):
                    a = 2 * math.pi * k / 12
                    poly.append(QPointF(x + r * 0.8 * math.cos(a), y + r * 0.8 * math.sin(a)))
            p.setPen(QPen(QColor(255, 255, 255), 1.4))
            p.setBrush(QColor(200, 50, 40) if hi else (QColor(30, 30, 30) if kind == "peak" else QColor(255, 255, 255)))
            p.drawPolygon(poly)
            if o["spot_labels"]:
                label(x * U, y * U, "%d %s" % (round(s[5]), scene.unit), r + 3, 0)
    if o["north"]:
        eye, right, up, fwd = cam.basis()
        nx = float(np.dot([0, 1, 0], right))
        ny = float(np.dot([0, 1, 0], up))
        c = QPointF(40, 40)
        p.setPen(QPen(QColor(*fg), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, 24, 24)
        tip = QPointF(c.x() + 20 * nx, c.y() - 20 * ny)
        p.drawLine(c, tip)
        p.setBrush(QColor(*fg))
        p.drawEllipse(tip, 3, 3)
        p.drawText(QPointF(c.x() - 4, c.y() - 30), "N")
    if o["legend"]:
        _legend(p, scene, o, h / U, fg)
    p.end()
    return out


def _legend(p, s, o, h, fg):
    h = int(h)
    mode, oo = o["mode"], s.to_out
    p.setPen(QColor(*fg))
    if mode in ("bands", "colours", "hill"):
        bar = QImage(120, 12, QImage.Format.Format_RGB32)
        for x in range(120):
            r, g, bl = hypso(x / 119.0, o["palette"])
            for y in range(12):
                bar.setPixelColor(x, y, QColor(int(r), int(g), int(bl)))
        x0, y0 = 14, h - 40
        p.drawText(QPointF(x0, y0 - 6), "Elevation (%s)" % s.unit)
        p.drawImage(x0, y0, bar)
        p.drawText(QPointF(x0, y0 + 26), "%d" % round(s.zmin * oo))
        p.drawText(QPointF(x0 + 96, y0 + 26), "%d" % round(s.zmax * oo))
        return
    items = ([(c[2], c[3]) for c in dem.SLOPE_CLASSES] if mode == "slope"
             else [(c[1], c[2]) for c in dem.ASPECT_CLASSES])
    x, y = 14, h - 28
    for label, colr in items:
        p.setBrush(QColor(colr))
        p.setPen(QPen(QColor(*fg), 1))
        p.drawRect(x, y, 12, 12)
        p.drawText(QPointF(x + 16, y + 11), label)
        x += 30 + p.fontMetrics().horizontalAdvance(label)


class Canvas(QWidget):
    def __init__(self, scene, zscale, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.cam = Camera(scene, zscale)
        self.opts = dict(DEFAULTS)
        if scene.contours is None:
            self.opts["contours"] = "off"
        if not scene.spots:
            self.opts.update(peaks=False, lows=False, highest=False)
        self.setMinimumSize(520, 420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img = None
        self._size = (0, 0)
        self._last = None
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(8)
        self._pending.timeout.connect(lambda: self.refresh(fast=True))
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(220)
        self._settle.timeout.connect(self.refresh)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag: rotate   Wheel: zoom   Double-click: reset view")

    def set_zscale(self, v):
        self.cam.cz *= v / self.cam.zscale
        self.cam.zscale = v
        self.refresh()

    def set_opt(self, key, value):
        self.opts[key] = value
        self.refresh()

    def request_fast(self):
        """Cheap redraw while moving (coalesced), full quality once the movement stops."""
        if not self._pending.isActive():
            self._pending.start()
        self._settle.start()

    def refresh(self, fast=False):
        self._pending.stop()
        if not fast:
            self._settle.stop()
        dpr = max(float(self.devicePixelRatioF()), 1.0)
        lw, lh = max(self.width(), 80), max(self.height(), 80)
        self._img = render(self.scene, self.cam, int(lw * dpr), int(lh * dpr), self.opts, fast=fast, dpr=dpr)
        self._img.setDevicePixelRatio(dpr)
        self._size = (self.width(), self.height())
        self.update()

    def image(self, w=1600, h=1000):
        return render(self.scene, self.cam, w, h, self.opts)

    def paintEvent(self, _e):
        if self._img is None or self._size != (self.width(), self.height()):
            self.refresh()
        p = QPainter(self)
        p.drawImage(0, 0, self._img)
        p.end()

    @staticmethod
    def _pos(e):
        return e.position() if hasattr(e, "position") else e.pos()

    def mousePressEvent(self, e):
        self._last = self._pos(e)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._last is None:
            return
        pos = self._pos(e)
        dx, dy = pos.x() - self._last.x(), pos.y() - self._last.y()
        self._last = pos
        self.cam.yaw = (self.cam.yaw - dx * 0.4) % 360.0
        self.cam.pitch = min(max(self.cam.pitch + dy * 0.3, 4.0), 88.0)
        self.request_fast()

    def mouseReleaseEvent(self, _e):
        self._last = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.request_fast()

    def wheelEvent(self, e):
        step = 0.88 if e.angleDelta().y() > 0 else 1.14
        self.cam.dist = min(max(self.cam.dist * step, self.scene.size_m * 0.15), self.scene.size_m * 6)
        self.request_fast()

    def mouseDoubleClickEvent(self, _e):
        self.cam.reset()
        self.refresh()


class Viewer(QDialog):
    MODES = [("Elevation bands", "bands"), ("Elevation colours", "colours"),
             ("Hillshaded colours", "hill"), ("Slope classes", "slope"), ("Aspect", "aspect")]
    CONTOURS = [("All contours", "all"), ("Index contours only", "index"), ("No contours", "off")]
    THEMES = [("Dark background", "dark"), ("Light background", "light"), ("White background", "white")]

    SHAPES = [("Smooth surface", "smooth"), ("Stepped bands (extruded)", "steps")]

    def __init__(self, scene, parent=None, title="AutoContour 3D"):
        super().__init__(parent)
        self.setWindowTitle(title)
        icon_file = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))
        # Same as the main AutoContour window: no parent, own taskbar entry, minimise button.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(1120, 720)
        zs = self.default_zscale(scene)
        self.canvas = Canvas(scene, zs)
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(self.canvas, 1)
        hint = QLabel("Drag to rotate  •  mouse wheel to zoom  •  double-click to reset")
        hint.setStyleSheet("color: #777;")
        left.addWidget(hint)
        root.addLayout(left, 1)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(QLabel("<b>Shape</b>"))
        side.addWidget(self._combo(self.SHAPES, "shape"))
        side.addWidget(QLabel("<b>Colours</b>"))
        self.mode = self._combo(self.MODES, "mode")
        side.addWidget(self.mode)
        side.addWidget(QLabel("Vertical exaggeration"))
        self.exag = QDoubleSpinBox()
        self.exag.setRange(0.5, 15.0)
        self.exag.setSingleStep(0.5)
        self.exag.setValue(zs)
        self.exag.setSuffix(" ×")
        self.exag.valueChanged.connect(self.canvas.set_zscale)
        side.addWidget(self.exag)
        side.addWidget(QLabel("<b>Show</b>"))
        has_c, has_s = scene.contours is not None, bool(scene.spots)
        self.cont = self._combo(self.CONTOURS, "contours")
        self.cont.setEnabled(has_c)
        side.addWidget(self.cont)
        side.addWidget(self._check("Contour elevations", "clabels", has_c))
        side.addWidget(self._check("Highest point", "highest", has_s))
        side.addWidget(self._check("Peaks", "peaks", has_s))
        side.addWidget(self._check("Low points", "lows", has_s))
        side.addWidget(self._check("Spot elevation labels", "spot_labels", has_s))
        side.addWidget(self._check("Terrain surface", "surface", True))
        side.addWidget(self._check("Sea", "sea", bool(scene.sea.any())))
        side.addWidget(self._check("Mesh grid", "wire", True))
        side.addWidget(self._check("Block with side walls", "block", True))
        side.addWidget(self._check("North arrow", "north", True))
        side.addWidget(self._check("Legend", "legend", True))
        side.addWidget(QLabel("<b>Look</b>"))
        side.addWidget(self._combo(self.THEMES, "theme"))
        side.addStretch(1)
        reset = QPushButton("Reset view")
        reset.clicked.connect(self._reset)
        save = QPushButton("Save image")
        save.clicked.connect(self._save)
        side.addWidget(reset)
        side.addWidget(save)
        panel = QWidget()
        panel.setLayout(side)
        panel.setFixedWidth(210)
        root.addWidget(panel)

    def _combo(self, items, key):
        box = QComboBox()
        for label, val in items:
            box.addItem(label, val)
        box.setCurrentIndex(max(box.findData(self.canvas.opts[key]), 0))
        box.currentIndexChanged.connect(lambda _i, b=box, k=key: self.canvas.set_opt(k, b.currentData()))
        return box

    def _check(self, label, key, enabled):
        cb = QCheckBox(label)
        cb.setChecked(bool(self.canvas.opts[key]) and enabled)
        cb.setEnabled(enabled)
        cb.toggled.connect(lambda on, k=key: self.canvas.set_opt(k, bool(on)))
        return cb

    @staticmethod
    def default_zscale(scene):
        frac = scene.relief / max(scene.size_m, 1.0)
        if frac >= 0.12:
            return 1.0
        return float(min(max(round(0.12 / max(frac, 1e-4) * 2) / 2.0, 1.0), 10.0))

    def _reset(self):
        self.canvas.cam.reset()
        self.canvas.refresh()

    def _save(self):
        path, _f = QFileDialog.getSaveFileName(self, "Save 3D image", "autocontour_3d.png", "PNG (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            self.canvas.image().save(path)


def open_viewer(parent, dem_path, dem_unit, unit, res, interval, sea_out, palette=DEFAULT_PALETTE):
    if not dem_path or not os.path.exists(dem_path):
        raise ValueError("Run the tool first: the 3D view uses the DEM that was just made.")
    scene = Scene(dem_path, dem_unit, unit, res.get("contours"), res.get("spots"), interval, sea_out)
    v = Viewer(scene, parent)
    v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    v.show()
    return v
