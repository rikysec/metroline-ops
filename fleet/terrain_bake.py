#!/usr/bin/env python3
"""terrain_bake.py — bake ELEVATION + WATER onto the exact geometry of a city's demand grid.

For every city grid (grid_<id>.json.gz produced by ghsl_bake.py) this writes terrain_<id>.json.gz with:
  elevation  : metres a.s.l. per cell (Copernicus GLO-30 DEM, 30 m → average onto the 100 m cells)
  water      : 1 when the cell centre lies inside an OSM water polygon (natural=water), or the cell is crossed
               by / within the half width (+10 m) of an OPEN river centreline (canals only if width ≥ 15 m;
               culverted/covered watercourses ignored), or the DEM reads ≤ 0 m (sea); else 0

Same cell indexing as the demand grid (north-up, originLat = north edge, dLat < 0, idx = row*nCols+col), so the
app's ConstructionCostModel can look up elevation, water and built-up density for a point with ONE cell index.

Sources (both public, read on the fly):
  DEM   https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_<N|S>lat_00_<E|W>lon_00_DEM/…_DEM.tif
  water https://overpass-api.de/api/interpreter (ways/relations natural=water, waterway=river, canal ≥ 15 m; no culverts)

Usage:
  python3 terrain_bake.py --grids SubwayTests/Fixtures/demand --out out_terrain [--cities torino,milano]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ghsl_bake  # noqa: E402  (read_raster_onto_grid)

DEM_URL = "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM/Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM.tif"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "metroline-terrain-bake/1.0"
DEFAULT_WIDTH_M = {"river": 30.0}
MARGIN_M = 10.0
MIN_CANAL_WIDTH_M = 15.0


def dem_tiles(bbox):
    min_lat, min_lon, max_lat, max_lon = bbox
    tiles = []
    for lat in range(int(math.floor(min_lat)), int(math.floor(max_lat)) + 1):
        for lon in range(int(math.floor(min_lon)), int(math.floor(max_lon)) + 1):
            la = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
            lo = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
            tiles.append(DEM_URL.format(lat=la, lon=lo))
    return tiles


def overpass_water(bbox, retries=3):
    min_lat, min_lon, max_lat, max_lon = bbox
    bb = f"({min_lat},{min_lon},{max_lat},{max_lon})"
    q = f"""[out:json][timeout:180];
(
  way["natural"="water"]{bb};
  relation["natural"="water"]{bb};
  way["waterway"~"^(river|canal)$"]{bb};
);
out geom;"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OVERPASS, data=("data=" + urllib.parse.quote(q)).encode(),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)["elements"]
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"  overpass attempt {attempt + 1} failed: {e}\n")
            time.sleep(15 * (attempt + 1))
    return None


def point_in_polygon(lat, lon, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def stitch_rings(ways):
    """Chain way fragments end-to-end into closed rings (OSM multipolygon outers)."""
    rings, pool = [], [list(w) for w in ways if len(w) >= 2]
    while pool:
        ring = pool.pop(0)
        changed = True
        while changed and ring[0] != ring[-1]:
            changed = False
            for i, w in enumerate(pool):
                if w[0] == ring[-1]:
                    ring += w[1:]; pool.pop(i); changed = True; break
                if w[-1] == ring[-1]:
                    ring += list(reversed(w))[1:]; pool.pop(i); changed = True; break
                if w[-1] == ring[0]:
                    ring = w[:-1] + ring; pool.pop(i); changed = True; break
                if w[0] == ring[0]:
                    ring = list(reversed(w))[:-1] + ring; pool.pop(i); changed = True; break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
        elif len(ring) >= 3:
            rings.append(ring + [ring[0]])   # open fragment chain (clipped by bbox): close it anyway
    return rings


def rasterize_water(elements, geom):
    n_rows, n_cols = geom["nRows"], geom["nCols"]
    o_lat, o_lon, d_lat, d_lon = geom["originLat"], geom["originLon"], geom["dLat"], geom["dLon"]
    water = [0] * (n_rows * n_cols)
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(o_lat + d_lat * n_rows / 2))

    def cell_center(r, c):
        return o_lat + (r + 0.5) * d_lat, o_lon + (c + 0.5) * d_lon

    def rows_cols_for(lats, lons, pad_m=0.0):
        pad_lat, pad_lon = pad_m / m_lat, pad_m / m_lon
        r1 = int(math.floor((max(lats) + pad_lat - o_lat) / d_lat)); r2 = int(math.ceil((min(lats) - pad_lat - o_lat) / d_lat))
        c1 = int(math.floor((min(lons) - pad_lon - o_lon) / d_lon)); c2 = int(math.ceil((max(lons) + pad_lon - o_lon) / d_lon))
        return max(0, min(r1, r2)), min(n_rows - 1, max(r1, r2)), max(0, c1), min(n_cols - 1, c2)

    polygons, lines = [], []
    for e in elements:
        tags = e.get("tags", {})
        if e["type"] == "way" and "geometry" in e:
            pts = [(p["lat"], p["lon"]) for p in e["geometry"]]
            if tags.get("natural") == "water" and len(pts) >= 4 and pts[0] == pts[-1]:
                polygons.append(pts)
            elif tags.get("waterway") in ("river", "canal"):
                # Covered / culverted watercourses (Milano's Seveso, Olona, 200 canal ways) are no obstacle.
                if "tunnel" in tags or tags.get("covered") == "yes" or str(tags.get("layer", "0")).startswith("-"):
                    continue
                w = tags.get("width")
                try:
                    w = float(str(w).replace(",", ".").split()[0]) if w else None
                except Exception:
                    w = None
                if tags["waterway"] == "canal" and (w is None or w < MIN_CANAL_WIDTH_M):
                    continue        # irrigation canals (rogge, derivatori) are a few metres wide: routine for a metro
                if w is None:
                    w = DEFAULT_WIDTH_M["river"]
                lines.append((pts, w / 2 + MARGIN_M))
        elif e["type"] == "relation":
            # multipolygon outers are usually split into several UNCLOSED ways: stitch them into rings
            polygons.extend(stitch_rings([[(p["lat"], p["lon"]) for p in m["geometry"]]
                                          for m in e.get("members", [])
                                          if m.get("type") == "way" and m.get("role") in ("outer", "") and "geometry" in m]))
    for ring in polygons:
        lats = [p[0] for p in ring]; lons = [p[1] for p in ring]
        r1, r2, c1, c2 = rows_cols_for(lats, lons)
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                la, lo = cell_center(r, c)
                if point_in_polygon(la, lo, ring):
                    water[r * n_cols + c] = 1
    for pts, half in lines:
        lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
        r1, r2, c1, c2 = rows_cols_for(lats, lons, pad_m=half)
        segs = list(zip(pts[:-1], pts[1:]))
        for (a, b) in segs:                      # supercover: every cell the centreline passes through
            L = math.hypot((b[0] - a[0]) * m_lat, (b[1] - a[1]) * m_lon)
            k = max(1, int(L / 20))
            for i in range(k + 1):
                la = a[0] + (b[0] - a[0]) * i / k; lo = a[1] + (b[1] - a[1]) * i / k
                r = int(math.floor((la - o_lat) / d_lat)); c = int(math.floor((lo - o_lon) / d_lon))
                if 0 <= r < n_rows and 0 <= c < n_cols:
                    water[r * n_cols + c] = 1
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                la, lo = cell_center(r, c)
                py, px = la * m_lat, lo * m_lon
                for (a, b) in segs:
                    ay, ax, by, bx = a[0] * m_lat, a[1] * m_lon, b[0] * m_lat, b[1] * m_lon
                    vx, vy = bx - ax, by - ay
                    L2 = vx * vx + vy * vy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
                    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
                    if dx * dx + dy * dy <= half * half:
                        water[r * n_cols + c] = 1
                        break
    return water, len(polygons), len(lines)


WORLDCOVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
WORLDCOVER_WATER_FRACTION = 0.20   # share of a 100 m cell covered by class 80 (permanent water) that flags the cell
WBM_WATER_FRACTION = 0.10          # share of the cell flagged by the GLO-30 water body mask (rivers 30 m, continuous lines)
# Calibrated on Torino against the OSM-based terrain fixture (862 water cells): WorldCover alone ≥ 0.35 → 382 cells,
# ≥ 0.20 → 539; the union with the GLO-30 WBM ≥ 0.10 → 843 cells, Jaccard 0.56 (the two sources draw the same rivers
# with different widths). WBM alone (2011–2015) misses nothing big in Torino but is older; WorldCover adds 2021 lakes.
GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.zip",
            "GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "5", "VSI_CACHE": "TRUE"}


def worldcover_tiles(bbox):
    """ESA WorldCover 3°×3° tiles named by their SW corner (N45E006 covers 6–9 E, 45–48 N)."""
    s, w, n, e = bbox
    out = []
    lat = 3 * math.floor(s / 3)
    while lat <= n:
        lon = 3 * math.floor(w / 3)
        while lon <= e:
            out.append(f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}")
            lon += 3
        lat += 3
    return out


def _mask_fraction(url, geom, bbox, is_water):
    """Fraction of each target cell covered by `is_water(pixels)` of a remote raster window (average resampling)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, Resampling
    from rasterio.windows import from_bounds, Window
    min_lat, min_lon, max_lat, max_lon = bbox
    res_deg = abs(geom["dLon"])
    dst_transform = from_origin(geom["originLon"], geom["originLat"], res_deg, res_deg)
    out = np.zeros((geom["nRows"], geom["nCols"]), dtype="float32")
    with rasterio.open(url) as src:
        px = abs(src.transform.a) * 2
        win = from_bounds(min_lon - px, min_lat - px, max_lon + px, max_lat + px, transform=src.transform)
        c0 = max(0, int(math.floor(win.col_off))); r0 = max(0, int(math.floor(win.row_off)))
        c1 = min(src.width, int(math.ceil(win.col_off + win.width))); r1 = min(src.height, int(math.ceil(win.row_off + win.height)))
        if c1 <= c0 or r1 <= r0:
            return out
        clamped = Window(c0, r0, c1 - c0, r1 - r0)
        mask = is_water(src.read(1, window=clamped)).astype("float32")
        reproject(source=mask, destination=out, src_transform=src.window_transform(clamped), src_crs=src.crs,
                  dst_transform=dst_transform, dst_crs="EPSG:4326", resampling=Resampling.average)
    return out


def worldcover_water(geom, bbox):
    """Water flags without Overpass: ESA WorldCover 2021 class 80 (permanent water; the sea is 80 near the coast and
    nodata 0 further out) covering ≥ WORLDCOVER_WATER_FRACTION of the 100 m cell, OR the Copernicus GLO-30 water
    body mask (ocean/lake/river, 30 m, continuous river lines) covering ≥ WBM_WATER_FRACTION. A missing tile (open
    ocean) leaves its cells to the DEM ≤ 0 rule. Returns the flat list and the number of tiles read."""
    import numpy as np
    import rasterio
    frac_wc = np.zeros((geom["nRows"], geom["nCols"]), dtype="float32")
    frac_wbm = np.zeros_like(frac_wc)
    tiles = 0
    with rasterio.Env(**GDAL_ENV):
        for tile in worldcover_tiles(bbox):
            try:
                frac_wc = np.maximum(frac_wc, _mask_fraction("/vsicurl/" + WORLDCOVER_URL.format(tile=tile), geom, bbox,
                                                             lambda d: (d == 80) | (d == 0)))
                tiles += 1
            except Exception as ex:  # noqa: BLE001 — a 404 is open ocean, anything else is reported and skipped
                print(f"  WorldCover {tile}: {ex}", flush=True)
        for url in dem_tiles(bbox):
            wbm = url.replace("_DEM.tif", "_WBM.tif").replace("_DEM/Copernicus", "_DEM/AUXFILES/Copernicus")
            try:
                frac_wbm = np.maximum(frac_wbm, _mask_fraction("/vsicurl/" + wbm, geom, bbox, lambda d: d > 0))
                tiles += 1
            except Exception as ex:  # noqa: BLE001
                print(f"  WBM {wbm.split('/')[-1]}: {ex}", flush=True)
    water = ((frac_wc >= WORLDCOVER_WATER_FRACTION) | (frac_wbm >= WBM_WATER_FRACTION)).astype("int64").flatten().tolist()
    return water, tiles


def bake_fleet(city_id, grids_dir, out_dir, bbox):
    """The unattended variant used by metroline-ops (no Overpass): GLO-30 elevation + WorldCover water + sea rule.
    Returns a small summary dict; writes terrain_<city>.json.gz atomically."""
    import numpy as np
    with gzip.open(os.path.join(grids_dir, f"grid_{city_id}.json.gz")) as f:
        g = json.load(f)
    geom = {k: g[k] for k in ("originLat", "originLon", "dLat", "dLon", "nRows", "nCols")}
    n = geom["nRows"] * geom["nCols"]
    elev = np.zeros((geom["nRows"], geom["nCols"]), dtype="float64")
    dem_ok = 0
    import rasterio
    with rasterio.Env(**GDAL_ENV):
        for url in dem_tiles(bbox):
            try:
                tile = ghsl_bake.read_raster_onto_grid("/vsicurl/" + url, geom, bbox, "average")
                elev = np.where(elev == 0, tile, elev)
                dem_ok += 1
            except Exception as ex:  # noqa: BLE001 — a missing GLO-30 tile is sea (elevation 0)
                print(f"  DEM {url.split('/')[-1]}: {ex}", flush=True)
    padded = np.pad(elev, 1, mode="edge")
    elev = np.min(np.stack([padded[dr:dr + elev.shape[0], dc:dc + elev.shape[1]] for dr in range(3) for dc in range(3)]), axis=0)
    water, wc_tiles = worldcover_water(geom, bbox)
    flat = elev.flatten().tolist()
    sea = 0
    for i, v in enumerate(flat):
        if v <= 0 and not water[i]:
            water[i] = 1; sea += 1
    out = {
        "cityId": city_id, "epoch": g.get("epoch"),
        "source": "Copernicus GLO-30 DSM (100 m avg, 3x3 min) + water: ESA WorldCover 2021 class 80 ≥ 20 % or GLO-30 WBM ≥ 10 % of the cell + sea (elev ≤ 0)",
        "cellSizeM": g.get("cellSizeM", 100), **geom,
        "elevation": [int(round(v)) for v in flat],
        "water": water,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"terrain_{city_id}.json.gz")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, path)
    return {"path": path, "cells": n, "waterCells": int(sum(water)), "seaCells": sea, "demTiles": dem_ok,
            "worldcoverTiles": wc_tiles, "bytes": os.path.getsize(path)}


def bake(city_id, grids_dir, out_dir, bbox):
    with gzip.open(os.path.join(grids_dir, f"grid_{city_id}.json.gz")) as f:
        g = json.load(f)
    geom = {k: g[k] for k in ("originLat", "originLon", "dLat", "dLon", "nRows", "nCols")}
    n = geom["nRows"] * geom["nCols"]
    import numpy as np
    elev = np.zeros((geom["nRows"], geom["nCols"]), dtype="float64")
    for url in dem_tiles(bbox):
        t = time.time()
        tile = ghsl_bake.read_raster_onto_grid(url, geom, bbox, "average")
        elev = np.where(elev == 0, tile, elev)
        print(f"  DEM {url.split('/')[-1]} {time.time() - t:.1f}s", flush=True)
    # GLO-30 is a SURFACE model (buildings + trees). Street level ≈ local 3×3 minimum of the 100 m averages:
    # an isolated tower cell (+30…60 m) disappears, hills and valleys (≥ 300 m wide) stay. Documented in the app.
    padded = np.pad(elev, 1, mode="edge")
    elev = np.min(np.stack([padded[dr:dr + elev.shape[0], dc:dc + elev.shape[1]] for dr in range(3) for dc in range(3)]), axis=0)
    cache = os.path.join(out_dir, f"water_{city_id}.osm.json")
    els = None
    if os.path.exists(cache):
        els = json.load(open(cache))
        print(f"  water: {len(els)} OSM elements (cached)", flush=True)
    else:
        els = overpass_water(bbox)
        if els is not None:
            os.makedirs(out_dir, exist_ok=True)
            json.dump(els, open(cache, "w"))
    if els is None:
        print("  WARNING: no water data (Overpass failed) — water layer empty", flush=True)
        water, np_, nl = [0] * n, 0, 0
    else:
        water, np_, nl = rasterize_water(els, geom)
    # The sea is not natural=water in OSM (natural=coastline): GLO-30 masks it to 0 m → sea cells = elevation ≤ 0.
    flat = elev.flatten().tolist()
    sea = 0
    for i, v in enumerate(flat):
        if v <= 0 and not water[i]:
            water[i] = 1; sea += 1
    if sea: print(f"  sea cells (elev ≤ 0): {sea}", flush=True)
    out = {
        "cityId": city_id, "epoch": g.get("epoch"), "source": "Copernicus GLO-30 DSM (100 m avg, 3x3 min) + OSM water (natural=water, waterway=river, canal ≥ 15 m; no culverts)",
        "cellSizeM": g.get("cellSizeM", 100), **geom,
        "elevation": [int(round(v)) for v in elev.flatten().tolist()],
        "water": water,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"terrain_{city_id}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    e = [v for v in out["elevation"] if v > 0]
    print(f"  → {path} cells={n} elev min/median/max={min(e) if e else 0}/{sorted(e)[len(e)//2] if e else 0}/{max(e) if e else 0} "
          f"water cells={sum(water)} ({100*sum(water)/n:.1f}%) polygons={np_} lines={nl} size={os.path.getsize(path)//1024} KB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grids", required=True, help="dir with grid_<id>.json.gz (same geometry)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(HERE, "cities.config.json"))
    ap.add_argument("--cities", default=None, help="comma-separated ids (default: all in config)")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    want = set(a.cities.split(",")) if a.cities else None
    for c in cfg["cities"]:
        if want and c["id"] not in want:
            continue
        print(f"== {c['id']}", flush=True)
        try:
            bake(c["id"], a.grids, a.out, c["bbox"])
        except FileNotFoundError as e:
            print(f"  skip: {e}")
        time.sleep(5)   # be gentle with Overpass


if __name__ == "__main__":
    main()
