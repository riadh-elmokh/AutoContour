"""Automatic choices: DEM source, contour interval, spot-elevation settings. No QGIS."""
import math

from . import demio

NICE_M = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
NICE_FT = (20, 50, 100, 200, 500, 1000, 2000, 5000)
TARGET_LEVELS = 30           # aim for at most about this many contour levels
M_PER_DEG = 111320.0
ARC_SECOND_M = 30.87


def area_km2(west, south, east, north):
    lat = math.radians((south + north) / 2.0)
    return abs(east - west) * abs(north - south) * (M_PER_DEG / 1000.0) ** 2 * math.cos(lat)


def cell_size_m(key, lat):
    """Approximate DEM cell size (metres, east-west, north-south)."""
    arc = demio.SOURCES[key]["native_deg"] * 3600.0
    return ARC_SECOND_M * arc * max(math.cos(math.radians(lat)), 0.05), ARC_SECOND_M * arc


TARGET_CELLS = 16000000      # the working grid is never larger than this


def estimate(key, west, south, east, north):
    """(tiles, native_cells, tile_limit, cell_m): cell_m is the cell size actually used."""
    src = demio.SOURCES[key]
    tiles = [t for t in demio.tiles_for(west, south, east, north)
             if src["lat"][0] <= t[0] < src["lat"][1]]
    dx, dy = cell_size_m(key, (south + north) / 2.0)
    cells = area_km2(west, south, east, north) * 1e6 / (dx * dy)
    scale = max(1.0, math.sqrt(cells / TARGET_CELLS))
    return len(tiles), cells, src["max_tiles"], dy * scale


def fits(key, west, south, east, north):
    tiles, _cells, limit, _cell_m = estimate(key, west, south, east, north)
    return 0 < tiles <= limit


def candidates(choice, west, south, east, north):
    """Ordered list of source keys to try. choice: 'auto' or a key. Large areas are
    read at a reduced resolution, so only the tile count limits a source."""
    order = ["cop30", "cop90"] if choice == "auto" else [choice, "cop30"]
    if choice not in demio.SOURCES and choice != "auto":
        return [choice]
    if choice in demio.SOURCES and "wcs" in demio.SOURCES[choice]:
        return [choice]
    out = []
    for k in order:
        if k not in out and fits(k, west, south, east, north):
            out.append(k)
    return out


def nice_interval(relief, unit):
    nice = NICE_FT if unit == "ft" else NICE_M
    for v in nice:
        if relief / v <= TARGET_LEVELS:
            return float(v)
    return float(nice[-1])


def spot_settings(cell_m, interval):
    """Search radius (cells) of about 1.5 km and a minimum relief of one interval."""
    radius = int(min(40, max(4, round(1500.0 / max(cell_m, 1.0)))))
    return radius, float(interval)
