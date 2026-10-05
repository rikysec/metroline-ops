#!/usr/bin/env python3
"""
GHSL → demand-grid bake pipeline (Metroline / Subway).

Turns the public JRC GHSL rasters into the per-city demand grids the three game
ports (Web / iOS / Android) already download and consume at runtime — WITHOUT any
engine change. This is the operator step that unlocks realistic per-catchment
demand (see Subway/AUDIT_LINEA2_P4_VALIDATION.md, sections "P4-bis" + "Operator
bake spec": real residents+jobs per cell move the model from geo-mean 0.15 to
~0.65-0.87 vs real metro ridership, with the formula unchanged).

INPUT  (public, JRC GHSL R2023A, free/attribution — https://ghsl.jrc.ec.europa.eu/download.php):
  --pop   GHS-POP            residents per 100 m cell        (residents signal)
  --nres  GHS-BUILT-V NRES   non-residential built volume m³ (jobs proxy)
  --cities  cities.config.json  (which cities to bake; bbox + optional employment)

OUTPUT (the contract in subwayweb/src/data/demand/ghslDemand.ts — shared by all ports):
  <out>/cities.json                 array of GhslCity (drives the city selector)
  <out>/grid_<id>.json[.gz]         one DemandGrid per city (summed over the catchment)

There is also a separate, STDLIB-ONLY `transit` subcommand (no numpy/rasterio):
  --feed  GTFS feed(s) (.zip or dir; stops/routes/trips/stop_times)
  → <out>/transit_index.json        array of {cityId,name,center,bbox,quality∈[0,1]}
It turns GTFS SERVICE DENSITY inside each city's bbox into the existing-transit `quality`
the P3 modal split consumes (subwayweb/src/data/demand/transitQuality.ts) — a city with
strong existing bus/tram/rail captures slightly LESS NEW metro demand. Uploads to demand/
alongside the grids; dormant until the file exists (no index ⇒ byte-identical binary logit).

The pure-Python core (grid assembly, jobs calibration, the catchment-sum port of
the engine's computeCatchmentDemand, AND the whole GTFS transit step) has NO third-party
deps so it is unit-testable anywhere; only the raster I/O needs numpy + rasterio. Run
`self-test` to validate the whole chain end-to-end on synthetic rasters + a synthetic GTFS
feed before touching real data.

Author: bake tool for the Linea2 demand engine. No credentials are embedded — the
Firebase upload is a separate operator command (see README.md).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
import zipfile
from typing import Optional

# ------------------------------------------------------------------------------------
# Constants — kept byte-identical to the runtime engine so a baked grid reads the same.
# ------------------------------------------------------------------------------------
EARTH_RADIUS_M = 6_371_000          # geo.ts EARTH_RADIUS_M (haversineDistance)
METERS_PER_DEG_LAT = 111_320        # ghslDemand.ts METERS_PER_DEG_LAT (catchment windowing)
DEFAULT_BUFFER_M = 800              # computeCatchmentDemand default bufferM (= 0.80 km catchment)
DEFAULT_CELL_M = 100                # GHSL native cell
DEFAULT_VOLUME_PER_WORKER_M3 = 200.0  # NRES built-volume → jobs (blended office/retail/industrial); CALIBRATE per city
DEFAULT_RESOLUTION_M = 100          # target grid cell size

# ------------------------------------------------------------------------------------
# GTFS → existing-transit quality (LINEA2 P3) — constants.
# ------------------------------------------------------------------------------------
# We turn raw GTFS SERVICE DENSITY inside a city's bbox into a single scalar
# quality ∈ [0,1] that the runtime modal split consumes as the THIRD (surface-transit)
# alternative (subwayweb/src/engine/modalSplit.ts P3: q multiplies the transit utility, so a
# higher q ⇒ existing bus/tram/rail captures more trips ⇒ slightly LESS NEW metro demand).
#
# The signal is a BLEND of three normalized service-density components, each divided by a
# defensible REFERENCE that a transit-rich urban core meets (→ component ≈ 1.0) and a sparse
# car-dependent city falls far short of (→ ≈ 0). The blend is then clamped to [0,1].
#
#   1. stop density        stops / km²
#   2. trip density        revenue trips/day / km²
#   3. mean route headway  → service frequency, mapped to [0,1] (10-min headway ⇒ ~1.0)
#
# References are order-of-magnitude anchors (a dense European tram/bus core), NOT precise
# calibration — adopt the STRUCTURE, tune the anchors per operator if needed. They are the
# values at which each component saturates to 1.0; everything is a ratio so the result is
# dimensionless and bounded.
GTFS_REF_STOPS_PER_KM2 = 12.0       # ~1 stop / 290 m grid → dense surface-transit core
GTFS_REF_TRIPS_PER_KM2 = 400.0      # revenue trips/day per km² at which trip-density saturates
GTFS_REF_HEADWAY_MIN = 10.0        # mean headway (min) that maps to frequency-score 1.0
# Blend weights (sum 1.0): coverage (stops) + intensity (trips) + service level (frequency).
GTFS_W_STOPS = 0.34
GTFS_W_TRIPS = 0.33
GTFS_W_FREQ = 0.33
GTFS_SERVICE_WINDOW_H = 18.0       # hours of a representative service day (for headway from trip count)


# ====================================================================================
# PURE CORE (stdlib only — unit-testable without numpy/rasterio)
# ====================================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Exact port of geo.ts haversineDistance (metres)."""
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def target_grid_for_bbox(bbox: list[float], res_deg: float) -> dict:
    """Define the canonical lat/lon target grid for a city bbox.

    bbox = [minLat, minLon, maxLat, maxLon] (the GhslCity contract order).
    Returns the grid geometry; row 0 is the NORTH edge so dLat < 0 (north-up), exactly
    what the engine assumes (cell (r,c) center: lat = originLat+(r+0.5)*dLat)."""
    min_lat, min_lon, max_lat, max_lon = bbox
    if not (max_lat > min_lat and max_lon > min_lon):
        raise ValueError(f"bbox must be [minLat,minLon,maxLat,maxLon] with max>min, got {bbox}")
    # ceil with a 1e-6-cell tolerance so float error on an exact division (0.2/0.01 →
    # 20.0000000002) doesn't spuriously add a row/col, while genuine partial cells still round up.
    n_cols = max(1, math.ceil((max_lon - min_lon) / res_deg - 1e-6))
    n_rows = max(1, math.ceil((max_lat - min_lat) / res_deg - 1e-6))
    return {
        "originLat": max_lat,   # north edge of row 0
        "originLon": min_lon,   # west edge of col 0
        "dLat": -res_deg,       # rows go south
        "dLon": res_deg,        # cols go east
        "nRows": n_rows,
        "nCols": n_cols,
    }


def nres_volume_to_jobs(nres_sum_m3: float,
                        volume_per_worker_m3: float,
                        employment_total: Optional[float]) -> float:
    """Raw jobs from non-residential built volume. If employment_total is given it is the
    target the per-cell jobs are calibrated to (caller scales the cell array accordingly)."""
    if volume_per_worker_m3 <= 0:
        raise ValueError("volume_per_worker_m3 must be > 0")
    raw = nres_sum_m3 / volume_per_worker_m3
    if employment_total is not None and raw > 0:
        return float(employment_total)
    return raw


def round_preserving_sum(rows: list[list[float]], target_total: float) -> list[list[int]]:
    """Round a 2-D float array to ints whose total EXACTLY equals round(target_total)
    (largest-remainder / Hare method). Plain per-cell rounding biases a calibrated field
    (e.g. 17k uniform cells of 2.86 each → 3 each = +4.8%); this hits the operator's known
    employment total exactly by handing the leftover units to the largest fractional parts."""
    n_rows = len(rows)
    n_cols = len(rows[0]) if n_rows else 0
    flat = [max(0.0, v) for row in rows for v in row]
    floors = [int(math.floor(v)) for v in flat]
    target = int(round(target_total))
    delta = target - sum(floors)
    if delta > 0:
        # add +1 to the `delta` cells with the largest fractional remainder
        order = sorted(range(len(flat)), key=lambda i: flat[i] - floors[i], reverse=True)
        for i in order[:delta]:
            floors[i] += 1
    elif delta < 0:
        # remove from the cells with the smallest remainder that still have a unit to give
        order = sorted(range(len(flat)), key=lambda i: flat[i] - floors[i])
        need = -delta
        for i in order:
            if need == 0:
                break
            if floors[i] > 0:
                floors[i] -= 1
                need -= 1
    return [floors[r * n_cols:(r + 1) * n_cols] for r in range(n_rows)]


def assemble_grid(city_id: str, epoch: int, source: str,
                  residents_rows: list[list[float]], jobs_rows: list[list[float]],
                  geom: dict, cell_size_m: int = DEFAULT_CELL_M) -> dict:
    """Build the DemandGrid dict (ghslDemand.ts) from 2-D residents/jobs arrays.
    residents/jobs are flattened ROW-MAJOR, rounded to ints (the engine sums ints)."""
    n_rows, n_cols = geom["nRows"], geom["nCols"]
    if len(residents_rows) != n_rows or len(jobs_rows) != n_rows:
        raise ValueError("residents/jobs row count must equal nRows")
    residents: list[int] = []
    jobs: list[int] = []
    for r in range(n_rows):
        if len(residents_rows[r]) != n_cols or len(jobs_rows[r]) != n_cols:
            raise ValueError("residents/jobs col count must equal nCols")
        for c in range(n_cols):
            residents.append(int(round(max(0.0, residents_rows[r][c]))))
            jobs.append(int(round(max(0.0, jobs_rows[r][c]))))
    return {
        "cityId": city_id,
        "epoch": epoch,
        "source": source,
        "cellSizeM": cell_size_m,
        "originLat": geom["originLat"],
        "originLon": geom["originLon"],
        "dLat": geom["dLat"],
        "dLon": geom["dLon"],
        "nRows": n_rows,
        "nCols": n_cols,
        "residents": residents,
        "jobs": jobs,
    }


def compute_catchment_demand(stations: list[tuple[float, float]], grid: dict,
                             buffer_m: float = DEFAULT_BUFFER_M) -> dict:
    """EXACT port of ghslDemand.ts computeCatchmentDemand — used to VERIFY a baked grid
    is consumable by the engine (and powers `--probe`). stations = [(lat, lon), ...]."""
    residents = 0
    jobs = 0
    cells = 0
    n_rows, n_cols = grid["nRows"], grid["nCols"]
    d_lat, d_lon = grid["dLat"], grid["dLon"]
    if not stations or n_rows <= 0 or n_cols <= 0 or d_lat == 0 or d_lon == 0:
        return {"residents": 0, "jobs": 0, "cellsCounted": 0}
    o_lat, o_lon = grid["originLat"], grid["originLon"]
    buf_lat = buffer_m / METERS_PER_DEG_LAT
    r_min = c_min = math.inf
    r_max = c_max = -math.inf
    for (lat, lon) in stations:
        buf_lon = buffer_m / (METERS_PER_DEG_LAT * max(0.05, math.cos(lat * math.pi / 180)))
        for la in (lat - buf_lat, lat + buf_lat):
            r = (la - o_lat) / d_lat - 0.5
            r_min, r_max = min(r_min, r), max(r_max, r)
        for lo in (lon - buf_lon, lon + buf_lon):
            c = (lo - o_lon) / d_lon - 0.5
            c_min, c_max = min(c_min, c), max(c_max, c)
    r0, r1 = max(0, math.floor(r_min)), min(n_rows - 1, math.ceil(r_max))
    c0, c1 = max(0, math.floor(c_min)), min(n_cols - 1, math.ceil(c_max))
    res_arr, job_arr = grid["residents"], grid["jobs"]
    for r in range(r0, r1 + 1):
        lat = o_lat + (r + 0.5) * d_lat
        for c in range(c0, c1 + 1):
            lon = o_lon + (c + 0.5) * d_lon
            inside = any(haversine_m(lat, lon, s_lat, s_lon) <= buffer_m for (s_lat, s_lon) in stations)
            if not inside:
                continue
            idx = r * n_cols + c
            residents += res_arr[idx] if idx < len(res_arr) else 0
            jobs += job_arr[idx] if idx < len(job_arr) else 0
            cells += 1
    return {"residents": residents, "jobs": jobs, "cellsCounted": cells}


def city_summary(city: dict, grid: dict) -> dict:
    """GhslCity (ghslDemand.ts): population = Σ residents, area from bbox, density derived."""
    min_lat, min_lon, max_lat, max_lon = city["bbox"]
    mid_lat = (min_lat + max_lat) / 2
    width_km = (max_lon - min_lon) * 111.320 * math.cos(mid_lat * math.pi / 180)
    height_km = (max_lat - min_lat) * 111.320
    area_km2 = max(1e-6, width_km * height_km)
    population = sum(grid["residents"])
    return {
        "id": city["id"],
        "name": city["name"],
        "country": city.get("country", ""),
        "center": [round(mid_lat, 6), round((min_lon + max_lon) / 2, 6)],
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "population": population if population > 0 else None,
        "areaKm2": round(area_km2, 3),
        # Integer like the canonical ghsl_pipeline.py (iOS decoded this as Int until 2026-10-03; now Double).
        "densityPerKm2": int(population / area_km2) if population > 0 else None,
    }


def serialize_grid(grid: dict, use_gzip: bool) -> bytes:
    raw = json.dumps(grid, separators=(",", ":")).encode("utf-8")
    if not use_gzip:
        return raw
    buf = io.BytesIO()
    # mtime=0 → deterministic output (same input ⇒ byte-identical file, friendly to CDN/etag).
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


# ====================================================================================
# GTFS → TRANSIT QUALITY (pure stdlib — zip/csv only; no third-party deps)
# ====================================================================================
# A GTFS feed is a zip (or a directory) of CSV files. We need only four:
#   stops.txt       stop_id, stop_lat, stop_lon
#   routes.txt      route_id, route_type
#   trips.txt       route_id, trip_id, service_id
#   stop_times.txt  trip_id, stop_id   (presence ⇒ the trip serves the stop)
# We do NOT parse the calendar — a feed's trips.txt already enumerates the scheduled trips of
# a representative service day at the granularity we need for a DENSITY signal. (Operators
# wanting strict weekday-only counts can pre-filter the feed; the blend is robust to it.)


def _open_gtfs_member(source: str, member: str):
    """Yield text lines for one GTFS member from a zip OR a directory. Returns None if absent.
    Tolerant of a UTF-8 BOM and of the file living at the zip root or one level down."""
    if os.path.isdir(source):
        path = os.path.join(source, member)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return f.read().splitlines()
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zf:
            names = zf.namelist()
            # exact, then any path ending in /member (feed nested in a sub-folder)
            target = next((n for n in names if n == member), None)
            if target is None:
                target = next((n for n in names if n.endswith("/" + member)), None)
            if target is None:
                return None
            raw = zf.read(target).decode("utf-8-sig", errors="replace")
            return raw.splitlines()
    return None


def _read_gtfs_table(source: str, member: str) -> list[dict]:
    """Parse a GTFS member into a list of dict rows (header-keyed). Empty list if absent.
    Header keys are stripped so a stray BOM/space never hides a column."""
    lines = _open_gtfs_member(source, member)
    if not lines:
        return []
    reader = csv.reader(lines)
    rows_iter = iter(reader)
    try:
        header = [h.strip() for h in next(rows_iter)]
    except StopIteration:
        return []
    out: list[dict] = []
    for raw in rows_iter:
        if not raw:
            continue
        out.append({header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))})
    return out


def _to_float(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def bbox_area_km2(bbox: list[float]) -> float:
    """Area of a [minLat,minLon,maxLat,maxLon] box in km² (cos-corrected at mid-latitude).
    Mirrors city_summary's width/height so transit density uses the SAME footprint as GHSL."""
    min_lat, min_lon, max_lat, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    width_km = (max_lon - min_lon) * 111.320 * math.cos(mid_lat * math.pi / 180)
    height_km = (max_lat - min_lat) * 111.320
    return max(1e-6, width_km * height_km)


def _in_bbox(lat: float, lon: float, bbox: list[float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = bbox
    return (min_lat <= lat <= max_lat) and (min_lon <= lon <= max_lon)


def gtfs_service_density(feeds: list[str], bbox: list[float]) -> dict:
    """Aggregate GTFS SERVICE DENSITY inside `bbox` across one or more feeds.

    Returns the raw, pre-normalization signals (so they are inspectable / testable):
      stopsInBbox     # of distinct stops whose coordinate falls inside the bbox
      tripsInBbox     # of revenue trips that touch >=1 in-bbox stop (a trip counts once)
      routesInBbox    # of distinct routes with >=1 in-bbox trip
      areaKm2         bbox area
      stopsPerKm2 / tripsPerKm2 / meanHeadwayMin   derived densities
    A trip 'serves' the bbox if any of its stop_times stops is inside — this captures lines
    that merely pass through as well as those wholly inside, which is the right denominator
    for 'how much existing service does a rider in this city see'."""
    in_stop: set[str] = set()           # stop_ids whose coords are inside the bbox (across feeds)
    trip_to_route: dict[str, str] = {}
    trip_in: set[str] = set()           # trip_ids that touch an in-bbox stop
    for feed in feeds:
        # NB stop_ids/trip_ids are namespaced per feed to avoid cross-feed id collisions.
        tag = os.path.basename(feed.rstrip("/")) or feed
        stops = _read_gtfs_table(feed, "stops.txt")
        local_in: set[str] = set()
        for s in stops:
            lat = _to_float(s.get("stop_lat"))
            lon = _to_float(s.get("stop_lon"))
            if lat is None or lon is None:
                continue
            if _in_bbox(lat, lon, bbox):
                sid = f"{tag}:{s.get('stop_id', '')}"
                local_in.add(sid)
                in_stop.add(sid)
        trips = _read_gtfs_table(feed, "trips.txt")
        for t in trips:
            tid = f"{tag}:{t.get('trip_id', '')}"
            trip_to_route[tid] = f"{tag}:{t.get('route_id', '')}"
        st = _read_gtfs_table(feed, "stop_times.txt")
        for r in st:
            sid = f"{tag}:{r.get('stop_id', '')}"
            if sid in local_in:
                trip_in.add(f"{tag}:{r.get('trip_id', '')}")

    routes_in = {trip_to_route.get(t, "") for t in trip_in}
    routes_in.discard("")
    area = bbox_area_km2(bbox)
    stops_n = len(in_stop)
    trips_n = len(trip_in)
    routes_n = len(routes_in)
    stops_per_km2 = stops_n / area
    trips_per_km2 = trips_n / area
    # Mean headway: SERVICE_WINDOW spread over the per-route trips. More trips/route ⇒ shorter
    # headway ⇒ better service. With no routes, headway is "infinite" (no service).
    trips_per_route = (trips_n / routes_n) if routes_n > 0 else 0.0
    mean_headway_min = (GTFS_SERVICE_WINDOW_H * 60.0 / trips_per_route) if trips_per_route > 0 else math.inf
    return {
        "stopsInBbox": stops_n,
        "tripsInBbox": trips_n,
        "routesInBbox": routes_n,
        "areaKm2": area,
        "stopsPerKm2": stops_per_km2,
        "tripsPerKm2": trips_per_km2,
        "meanHeadwayMin": mean_headway_min,
    }


def transit_quality_from_density(density: dict) -> float:
    """Blend the raw service-density signals into quality ∈ [0,1] (CLAMPED).

    Each component is a saturating ratio against a transit-rich reference:
      coverage  = min(1, stopsPerKm2 / REF_STOPS)
      intensity = min(1, tripsPerKm2 / REF_TRIPS)
      frequency = min(1, REF_HEADWAY / meanHeadwayMin)   (shorter headway ⇒ closer to 1)
    quality = clamp01( w_s·coverage + w_t·intensity + w_f·frequency ).
    Monotonic in every signal (more stops / more trips / shorter headway ⇒ same-or-higher q),
    and bounded to [0,1] by construction."""
    coverage = min(1.0, max(0.0, density["stopsPerKm2"]) / GTFS_REF_STOPS_PER_KM2)
    intensity = min(1.0, max(0.0, density["tripsPerKm2"]) / GTFS_REF_TRIPS_PER_KM2)
    headway = density["meanHeadwayMin"]
    frequency = 0.0 if (headway == math.inf or headway <= 0) else min(1.0, GTFS_REF_HEADWAY_MIN / headway)
    q = GTFS_W_STOPS * coverage + GTFS_W_TRIPS * intensity + GTFS_W_FREQ * frequency
    return min(1.0, max(0.0, q))


def transit_entry_for_city(city: dict, feeds: list[str]) -> dict:
    """Compute one TransitQualityEntry {cityId,name,center,bbox,quality} for a city from GTFS.
    Output contract MUST match subwayweb/src/data/demand/transitQuality.ts TransitQualityEntry."""
    bbox = [float(x) for x in city["bbox"]]
    min_lat, min_lon, max_lat, max_lon = bbox
    density = gtfs_service_density(feeds, bbox)
    quality = transit_quality_from_density(density)
    return {
        "cityId": city["id"],
        "name": city["name"],
        "center": [round((min_lat + max_lat) / 2, 6), round((min_lon + max_lon) / 2, 6)],
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "quality": round(quality, 4),
    }


def serialize_transit_index(entries: list[dict], use_gzip: bool) -> bytes:
    """Serialize the transit index (array of TransitQualityEntry). gzip optional + deterministic."""
    raw = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not use_gzip:
        return raw
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


# ====================================================================================
# RASTER I/O (numpy + rasterio) — only imported when actually baking real rasters.
# ====================================================================================

def _require_rasterio():
    try:
        import numpy as np  # noqa: F401
        import rasterio  # noqa: F401
        from rasterio.warp import reproject, Resampling, transform_bounds  # noqa: F401
        from rasterio.windows import from_bounds  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover - environment dependent
        sys.stderr.write(
            f"ERROR: raster baking needs numpy + rasterio ({e}).\n"
            f"  pip install -r requirements.txt\n"
            f"The pure-core tests and --self-test (synthetic) still need rasterio for I/O.\n"
        )
        return False


def read_raster_onto_grid(path: str, geom: dict, bbox: list[float], resampling_name: str):
    """Reproject/resample a source raster (any CRS) onto the city's lat/lon target grid.

    Reads only a WINDOW around the bbox so a global GHSL tile never loads whole. Negatives,
    NaN and nodata are flattened to 0. Returns a (nRows, nCols) numpy float array aligned to
    `geom`. Population/jobs are COUNT-like, so for matched ~100 m resolution use 'nearest'
    (default) — it neither invents nor drops mass; only switch to 'average' for true
    downsampling of a finer source (see README)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.windows import from_bounds

    resampling = getattr(Resampling, resampling_name)
    min_lat, min_lon, max_lat, max_lon = bbox
    res_deg = abs(geom["dLon"])
    n_rows, n_cols = geom["nRows"], geom["nCols"]
    dst_transform = from_origin(geom["originLon"], geom["originLat"], res_deg, res_deg)
    dst = np.zeros((n_rows, n_cols), dtype="float64")

    with rasterio.open(path) as src:
        # bbox → source CRS bounds (densified so curved reprojections stay enclosing), padded 2 cells.
        l, b, r, t = transform_bounds("EPSG:4326", src.crs,
                                      min_lon, min_lat, max_lon, max_lat, densify_pts=21)
        # pad by 2 source pixels
        px = abs(src.transform.a) * 2
        py = abs(src.transform.e) * 2
        win = from_bounds(l - px, b - py, r + px, t + py, transform=src.transform)
        # clamp window to the raster extent (floor the offset, ceil the far edge; version-agnostic).
        col_off = max(0, int(math.floor(win.col_off)))
        row_off = max(0, int(math.floor(win.row_off)))
        col_end = min(src.width, int(math.ceil(win.col_off + win.width)))
        row_end = min(src.height, int(math.ceil(win.row_off + win.height)))
        if col_end <= col_off or row_end <= row_off:
            sys.stderr.write(f"WARN: {os.path.basename(path)} has no data over bbox {bbox}; zeros.\n")
            return dst
        from rasterio.windows import Window
        clamped = Window(col_off, row_off, col_end - col_off, row_end - row_off)
        data = src.read(1, window=clamped).astype("float64")
        win_transform = src.window_transform(clamped)

        if src.nodata is not None:
            data = np.where(data == src.nodata, 0.0, data)
        data = np.where(np.isfinite(data), data, 0.0)
        data = np.where(data < 0, 0.0, data)

        reproject(
            source=data, destination=dst,
            src_transform=win_transform, src_crs=src.crs,
            dst_transform=dst_transform, dst_crs="EPSG:4326",
            resampling=resampling,
        )

    dst = np.where(np.isfinite(dst), dst, 0.0)
    dst = np.where(dst < 0, 0.0, dst)
    return dst


def bake_city(city: dict, pop_path: str, nres_path: str, *,
              epoch: int, source: str, res_m: float,
              volume_per_worker_m3: float, resampling_name: str) -> tuple[dict, dict]:
    """Bake one city → (DemandGrid, GhslCity). Jobs = NRES volume / volume_per_worker, then
    (optionally) calibrated so Σ jobs == city['employmentTotal']."""
    res_deg = res_m / METERS_PER_DEG_LAT
    geom = target_grid_for_bbox(city["bbox"], res_deg)
    pop = read_raster_onto_grid(pop_path, geom, city["bbox"], resampling_name)
    nres = read_raster_onto_grid(nres_path, geom, city["bbox"], resampling_name)

    raw_jobs = nres / float(volume_per_worker_m3)
    employment_total = city.get("employmentTotal")
    raw_sum = float(raw_jobs.sum())

    residents_rows = [[float(v) for v in row] for row in pop]
    jobs_rows_f = [[float(v) for v in row] for row in raw_jobs]
    if employment_total is not None and raw_sum > 0:
        # scale to the known total, then largest-remainder round so Σjobs == employmentTotal exactly.
        scale = float(employment_total) / raw_sum
        jobs_rows = round_preserving_sum([[v * scale for v in row] for row in jobs_rows_f],
                                         float(employment_total))
    else:
        jobs_rows = jobs_rows_f  # uncalibrated; assemble_grid does the per-cell rounding
    cell_m = int(round(res_deg * METERS_PER_DEG_LAT))
    grid = assemble_grid(city["id"], epoch, source, residents_rows, jobs_rows, geom, cell_m)
    return grid, city_summary(city, grid)


# ====================================================================================
# DRIVER
# ====================================================================================

def write_outputs(out_dir: str, cities_out: list[dict], grids: list[dict], use_gzip: bool) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "cities.json"), "w", encoding="utf-8") as f:
        json.dump(cities_out, f, ensure_ascii=False, indent=2)
    for grid in grids:
        name = f"grid_{grid['cityId']}.json" + (".gz" if use_gzip else "")
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(serialize_grid(grid, use_gzip))


def run_bake(args) -> int:
    if not _require_rasterio():
        return 2
    with open(args.cities, encoding="utf-8") as f:
        cfg = json.load(f)
    epoch = int(cfg.get("epoch", args.epoch))
    source = cfg.get("source", "GHS-POP R2023A + GHS-BUILT-V R2023A NRES")
    vpw = float(cfg.get("volumePerWorkerM3", args.volume_per_worker))
    res_m = float(cfg.get("resolutionM", args.res_m))
    cities = cfg["cities"]

    cities_out, grids = [], []
    for city in cities:
        sys.stderr.write(f"baking {city['id']} ({city.get('name','')}) …\n")
        grid, summary = bake_city(
            city, args.pop, args.nres,
            epoch=epoch, source=source, res_m=res_m,
            volume_per_worker_m3=vpw, resampling_name=args.resampling,
        )
        pop = sum(grid["residents"])
        job = sum(grid["jobs"])
        sys.stderr.write(
            f"  grid {grid['nRows']}×{grid['nCols']}  residents={pop:,}  jobs={job:,}"
            f"  density≈{summary['densityPerKm2']}\n"
        )
        cities_out.append(summary)
        grids.append(grid)

    write_outputs(args.out, cities_out, grids, use_gzip=not args.no_gzip)
    sys.stderr.write(
        f"\nWrote {len(grids)} grid(s) + cities.json to {args.out}\n"
        f"Next: upload {args.out}/ to Firebase Storage under the `demand/` prefix (see README.md).\n"
    )
    return 0


def run_probe(args) -> int:
    """Sum residents/jobs over an ad-hoc catchment of a baked grid — sanity-checks output."""
    path = args.grid
    raw = open(path, "rb").read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    grid = json.loads(raw)
    stations = [tuple(float(x) for x in s.split(",")) for s in args.station]
    out = compute_catchment_demand(stations, grid, buffer_m=args.buffer)
    print(json.dumps({"grid": os.path.basename(path), "stations": stations,
                      "bufferM": args.buffer, **out}, indent=2))
    return 0


def run_transit(args) -> int:
    """GTFS → transit_index.json. Reads one or more GTFS feeds (zip or dir) and emits a per-city
    existing-transit quality ∈ [0,1] from service density inside each city's bbox. Pure stdlib —
    no numpy/rasterio needed (only zip/csv). Output = the contract transitQuality.ts consumes."""
    with open(args.cities, encoding="utf-8") as f:
        cfg = json.load(f)
    cities = cfg["cities"]
    feeds = list(args.feed)
    for feed in feeds:
        if not (os.path.isdir(feed) or zipfile.is_zipfile(feed)):
            sys.stderr.write(f"ERROR: --feed {feed} is neither a directory nor a zip GTFS feed.\n")
            return 2

    entries: list[dict] = []
    for city in cities:
        sys.stderr.write(f"transit {city['id']} ({city.get('name','')}) …\n")
        density = gtfs_service_density(feeds, [float(x) for x in city["bbox"]])
        entry = transit_entry_for_city(city, feeds)
        hw = density["meanHeadwayMin"]
        hw_s = "∞" if hw == math.inf else f"{hw:.1f}"
        sys.stderr.write(
            f"  stops={density['stopsInBbox']}  trips={density['tripsInBbox']}"
            f"  routes={density['routesInBbox']}  stops/km²={density['stopsPerKm2']:.2f}"
            f"  trips/km²={density['tripsPerKm2']:.1f}  headway={hw_s}min  → quality={entry['quality']}\n"
        )
        entries.append(entry)

    os.makedirs(args.out, exist_ok=True)
    name = "transit_index.json" + (".gz" if args.gzip else "")
    with open(os.path.join(args.out, name), "wb") as f:
        f.write(serialize_transit_index(entries, use_gzip=args.gzip))
    sys.stderr.write(
        f"\nWrote {len(entries)} transit entr{'y' if len(entries)==1 else 'ies'} to {args.out}/{name}\n"
        f"Next: upload it to Firebase Storage under the `demand/` prefix (see README.md) — same\n"
        f"folder as the grids. The runtime (transitQuality.ts) downloads demand/transit_index.json once.\n"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Bake GHSL rasters → Metroline demand grids.")
    sub = p.add_subparsers(dest="cmd", required=False)

    pb = sub.add_parser("bake", help="bake cities.config.json into grids + cities.json")
    pb.add_argument("--pop", required=True, help="GHS-POP raster (residents)")
    pb.add_argument("--nres", required=True, help="GHS-BUILT-V NRES raster (jobs proxy)")
    pb.add_argument("--cities", required=True, help="cities.config.json")
    pb.add_argument("--out", required=True, help="output directory")
    pb.add_argument("--epoch", type=int, default=2025)
    pb.add_argument("--volume-per-worker", type=float, default=DEFAULT_VOLUME_PER_WORKER_M3)
    pb.add_argument("--res-m", type=float, default=DEFAULT_RESOLUTION_M)
    pb.add_argument("--resampling", default="nearest", choices=["nearest", "average", "bilinear"])
    pb.add_argument("--no-gzip", action="store_true", help="emit plain .json (default: .json.gz)")
    pb.set_defaults(func=run_bake)

    pp = sub.add_parser("probe", help="sum a catchment of a baked grid (verification)")
    pp.add_argument("--grid", required=True, help="grid_<id>.json[.gz]")
    pp.add_argument("--station", action="append", required=True, metavar="LAT,LON",
                    help="repeatable; e.g. --station 45.07,7.68")
    pp.add_argument("--buffer", type=float, default=DEFAULT_BUFFER_M)
    pp.set_defaults(func=run_probe)

    pt = sub.add_parser("transit", help="GTFS feed(s) → demand/transit_index.json (existing-transit quality)")
    pt.add_argument("--feed", action="append", required=True, metavar="ZIP_OR_DIR",
                    help="repeatable; a GTFS feed as a .zip or an unzipped directory")
    pt.add_argument("--cities", required=True, help="cities.config.json (reuses the bbox per city)")
    pt.add_argument("--out", required=True, help="output directory (writes transit_index.json[.gz])")
    pt.add_argument("--gzip", action="store_true", help="emit transit_index.json.gz (default: plain .json)")
    pt.set_defaults(func=run_transit)

    ps = sub.add_parser("self-test", help="validate the whole chain on synthetic rasters")
    ps.set_defaults(func=lambda a: _run_self_test())

    # back-compat: bare `--self-test`
    p.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args(argv)
    if getattr(args, "self_test", False) or args.cmd == "self-test":
        return _run_self_test()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)


def _run_self_test() -> int:
    """Delegate to the test module so `ghsl_bake.py self-test` works standalone."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import test_ghsl_bake
    return test_ghsl_bake.run_all()


if __name__ == "__main__":
    raise SystemExit(main())
