"""Elevation processing on a numpy grid. No QGIS imports.

All functions work on z[row, col] with NaN for no-data.
"""
import numpy as np

FT_PER_M = 3.28084
MAX_CANDIDATES = 200000


def to_unit(z, from_unit, to_unit):
    """Convert elevations between 'm' and 'ft'."""
    if from_unit == to_unit:
        return z
    return z * FT_PER_M if to_unit == "ft" else z / FT_PER_M


def _run_filter(z, r, fill, op):
    """Separable running max/min over a (2r+1) square window."""
    nr, nc = z.shape
    w = np.where(np.isfinite(z), z, fill)
    pad = np.pad(w, ((r, r), (0, 0)), mode="constant", constant_values=fill)
    out = pad[0:nr, :].copy()
    for k in range(1, 2 * r + 1):
        out = op(out, pad[k:k + nr, :])
    pad = np.pad(out, ((0, 0), (r, r)), mode="constant", constant_values=fill)
    res = pad[:, 0:nc].copy()
    for k in range(1, 2 * r + 1):
        res = op(res, pad[:, k:k + nc])
    return res


def max_filter(z, r):
    return _run_filter(z, r, -np.inf, np.maximum)


def min_filter(z, r):
    return _run_filter(z, r, np.inf, np.minimum)


def _dedupe(cands, z, r):
    """Collapse each plateau (touching cells of equal height) to the cell
    nearest its centre, then keep the highest of any spots closer than r."""
    cset = set(cands)
    seen = set()
    reps = []
    for start in sorted(cands):
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            i, j = stack.pop()
            group.append((i, j))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (i + di, j + dj)
                    if nb in cset and nb not in seen and z[nb] == z[start]:
                        seen.add(nb)
                        stack.append(nb)
        ci = sum(p[0] for p in group) / float(len(group))
        cj = sum(p[1] for p in group) / float(len(group))
        reps.append(min(group, key=lambda p: ((p[0] - ci) ** 2 + (p[1] - cj) ** 2, p)))
    order = sorted(reps, key=lambda ij: (-z[ij], ij[0], ij[1]))
    cell = max(r, 1)
    taken = {}
    kept = []
    for i, j in order:
        ci, cj = i // cell, j // cell
        clash = False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (pi, pj) in taken.get((ci + di, cj + dj), ()):
                    if abs(pi - i) <= r and abs(pj - j) <= r:
                        clash = True
        if not clash:
            kept.append((i, j))
            taken.setdefault((ci, cj), []).append((i, j))
    return kept


def find_spots(z, radius, min_relief, want_peaks=True, want_lows=False, sea=None):
    """Local maxima (peaks) and minima (lows).

    radius     : search radius in cells; a spot is the extreme of its window
    min_relief : minimum height difference to the lowest (peak) / highest (low)
                 cell within 3 x radius, in the units of z
    Cells closer than `radius` to the grid edge are skipped.
    sea        : optional boolean mask of sea cells: never a spot, never a reference
    Returns list of (row, col, value, kind).
    """
    z = np.asarray(z, dtype=float)
    r = int(radius)
    if r < 1:
        raise ValueError("Search radius must be at least 1 cell.")
    nr, nc = z.shape
    out = []
    margin = np.zeros_like(z, dtype=bool)
    margin[r:nr - r, r:nc - r] = True
    big = 3 * r
    jobs = []
    if want_peaks:
        jobs.append(("peak", z if sea is None else np.where(sea, np.nanmin(np.where(sea, np.nan, z)), z)))
    if want_lows:
        jobs.append(("low", -z if sea is None else np.where(sea, -np.nanmax(np.where(sea, np.nan, z)), -z)))
    for kind, zz in jobs:
        finite = np.isfinite(zz)
        mx = max_filter(zz, r)
        mn = min_filter(zz, big)
        with np.errstate(invalid="ignore"):
            cand = finite & margin & (zz == mx) & ((zz - mn) >= min_relief)
            if sea is not None:
                cand &= ~sea
                if kind == "low":      # next to the sea the lowest land is the shore, not a pit
                    cand &= max_filter(sea.astype(float), r) == 0
        ii, jj = np.nonzero(cand)
        if ii.size > MAX_CANDIDATES:
            raise ValueError(
                "Too many candidate spots (%d). Increase the search radius or "
                "the minimum relief." % ii.size)
        kept = _dedupe(list(zip(ii.tolist(), jj.tolist())), zz, r)
        for i, j in kept:
            out.append((i, j, float(z[i, j]), kind))
    return out


def highest_point(z):
    """(row, col, value) of the highest finite cell, or None."""
    z = np.asarray(z, dtype=float)
    if not np.isfinite(z).any():
        return None
    idx = int(np.nanargmax(np.where(np.isfinite(z), z, -np.inf)))
    i, j = divmod(idx, z.shape[1])
    return i, j, float(z[i, j])


def contour_levels(zmin, zmax, interval, base=0.0):
    first = np.ceil((zmin - base) / interval) * interval + base
    n = int(np.floor((zmax - first) / interval + 1e-9)) + 1
    return [round(float(first + k * interval), 6) for k in range(max(n, 0))]


def stats(z):
    f = z[np.isfinite(z)]
    if f.size == 0:
        return None
    return {"min": float(f.min()), "max": float(f.max()),
            "nodata_pct": 100.0 * (1.0 - f.size / z.size)}


def hillshade(z, dx, dy, azimuth=315.0, altitude=45.0):
    """Hillshade 0-255 (uint8, 0 = no data). dx, dy: cell size in metres."""
    z = np.asarray(z, dtype=float)
    fill = np.where(np.isfinite(z), z, np.nanmean(z) if np.isfinite(z).any() else 0.0)
    gy, gx = np.gradient(fill, dy, dx)       # rows go south: dy is positive spacing
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)
    az = np.radians(360.0 - azimuth + 90.0)
    zen = np.radians(90.0 - altitude)
    hs = (np.cos(zen) * np.cos(slope)
          + np.sin(zen) * np.sin(slope) * np.cos(az - aspect))
    out = np.clip(hs * 254.0, 0, 254).astype(np.uint8) + 1
    out[~np.isfinite(z)] = 0
    return out


SLOPE_CLASSES = [(0, 5, "0-5°", "#d9f0d3"), (5, 15, "5-15°", "#a6d96a"),
                 (15, 30, "15-30°", "#fee08b"), (30, 45, "30-45°", "#f46d43"),
                 (45, 91, "over 45°", "#a50026")]
ASPECT_CLASSES = [(1, "Flat", "#cccccc"), (2, "N", "#4575b4"), (3, "NE", "#91bfdb"),
                  (4, "E", "#e0f3f8"), (5, "SE", "#fee090"), (6, "S", "#fc8d59"),
                  (7, "SW", "#d73027"), (8, "W", "#f4a582"), (9, "NW", "#92c5de")]


def slope_aspect(z, dx, dy):
    """Slope (degrees) and aspect (degrees clockwise from north, direction the slope faces).
    dx, dy: cell size in metres. NaN where z is NaN."""
    z = np.asarray(z, dtype=float)
    ok = np.isfinite(z)
    fill = np.where(ok, z, np.nanmean(z) if ok.any() else 0.0)
    gy, gx = np.gradient(fill, dy, dx)      # gy: change per metre going south
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = np.degrees(np.arctan2(-gx, gy)) % 360.0
    slope[~ok] = np.nan
    aspect[~ok] = np.nan
    return slope, aspect


def slope_classes(slope):
    out = np.zeros(slope.shape, dtype=np.uint8)
    for k, (lo, hi, _label, _col) in enumerate(SLOPE_CLASSES, start=1):
        out[(slope >= lo) & (slope < hi)] = k
    return out


def aspect_classes(aspect, slope, flat_deg=1.0):
    """1 = flat, 2..9 = N, NE, E, SE, S, SW, W, NW; 0 = no data."""
    sector = (((np.nan_to_num(aspect) + 22.5) % 360.0) // 45.0).astype(int) + 2
    out = np.where(slope < flat_deg, 1, sector)
    out = np.where(np.isfinite(aspect) & np.isfinite(slope), out, 0)
    return out.astype(np.uint8)
