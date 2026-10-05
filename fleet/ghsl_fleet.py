#!/usr/bin/env python3
"""
ghsl_fleet.py — bakes the REAL-DATA files of the world's most populous cities on the data host, unattended:
    grid_<city>.json.gz     residents (GHS-POP E2025 R2023A) + jobs (GHS-BUILT-V NRES E2025 R2023A), 100 m cells
    terrain_<city>.json.gz  elevation (Copernicus GLO-30) + water (ESA WorldCover 2021 class 80) + sea
    cities.json             the catalogue the app downloads first (the 8 hand-baked cities verbatim + every baked city)

    python3 fleet/ghsl_fleet.py [--budget-minutes 50] [--max-cities 60] [--only id,id] [--catalogue-only]

How: cities are grouped by the GHSL WGS84 tile(s) their bbox touches (348 land tiles of 10°×10°, fleet/ghsl_tiles_4326.json);
per tile the two zips are downloaded once (POP up to ~340 MB, NRES ≤ 15 MB; HTTP Range resume) and every queued city
of that tile is baked from local windows (/vsizip). Jobs total: the pinned cities keep their calibrated
employmentTotal; the others use Σresidents × (1 − share 0–14) × employment-to-population ratio × 1.4 (World Bank
SP.POP.0014.TO.ZS / SL.EMP.TOTL.SP.ZS, cached a year; MAPE 13 % on the 8 reference cities). Terrain reads GLO-30
and WorldCover windows over HTTPS (no Overpass). Files land in the served folder atomically; cities.json is rebuilt
after every city. Never schedules work past its deadline. Raster I/O needs numpy + rasterio: the script creates a
venv in the state dir on first run (Xcode CLT python 3.9, pinned wheels, no sudo) and re-executes itself there.

Paths (env, set by the agent): METROLINE_OPS_REPO, METROLINE_OPS_STATE (~/metroline-ops-state), METROLINE_DATA
(~/metroline-data/demand). GHSL R2023A © European Commission, JRC — CC BY 4.0; WorldCover © ESA — CC BY 4.0;
GLO-30 © DLR/Airbus via Copernicus.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HOME = os.path.expanduser("~")
REPO = os.environ.get("METROLINE_OPS_REPO", os.path.dirname(HERE))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
VENV = os.path.join(STATE, "venv")
TILE_CACHE = os.path.join(STATE, "ghsl_tiles")
STATE_FILE = os.path.join(STATE, "ghsl_fleet.json")
SUMMARY_FILE = os.path.join(STATE, "ghsl_fleet_summary.json")
WB_FILE = os.path.join(STATE, "worldbank.json")
BAKE_VERSION = 1                 # bump to re-bake every non-pinned city with new rules
TILE_CACHE_MAX_BYTES = 3 * 1024 ** 3
CITY_RESERVE_S = 6 * 60          # do not start a city with less than this left (a POP tile download can take minutes)
JRC = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL"
PRODUCTS = {
    "pop": ("GHS_POP_GLOBE_R2023A", "GHS_POP_E2025_GLOBE_R2023A_4326_3ss"),
    "nres": ("GHS_BUILT_V_GLOBE_R2023A", "GHS_BUILT_V_NRES_E2025_GLOBE_R2023A_4326_3ss"),
}
JOBS_C = 1.4                     # calibration constant of the automatic employment rule (see research 2026-10-05)
EPOCH, RES_M, SOURCE = 2025, 100, "GHS-POP R2023A + GHS-BUILT-V R2023A NRES"
UA = "MetrolineGHSLBot/1.0 (+https://metroline.app)"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def write_bytes_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- environment
def ensure_venv():
    """numpy + rasterio from pinned wheels in STATE/venv (idempotent: a stamp of the requirements file). Returns the
    venv python. Any failure is fatal for this run (reported by the agent)."""
    req = os.path.join(HERE, "requirements-bake.txt")
    stamp_want = hashlib.sha256(open(req, "rb").read()).hexdigest()[:16]
    py = os.path.join(VENV, "bin", "python3")
    stamp = os.path.join(VENV, ".ok-" + stamp_want)
    if os.path.exists(py) and os.path.exists(stamp):
        if subprocess.run([py, "-c", "import rasterio, numpy"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return py
        print("venv broken (Xcode/CLT moved?) — rebuilding", flush=True)
        shutil.rmtree(VENV, ignore_errors=True)
    base = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    if subprocess.run(["xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0 and base == "/usr/bin/python3":
        raise SystemExit("Command Line Tools absent: /usr/bin/python3 would open the installer dialog — install CLT or Homebrew python3")
    print(f"creating venv at {VENV} with {base}", flush=True)
    subprocess.run([base, "-m", "venv", VENV], check=True)
    subprocess.run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    subprocess.run([py, "-m", "pip", "install", "--quiet", "--only-binary", ":all:", "--no-cache-dir", "-r", req], check=True)
    smoke = ("import rasterio, numpy; from rasterio.warp import transform_bounds; "
             "assert rasterio.__gdal_version__.startswith('3.'), rasterio.__gdal_version__; "
             "transform_bounds('EPSG:4326','ESRI:54009',7,45,8,46); print('rasterio', rasterio.__version__, 'gdal', rasterio.__gdal_version__, 'numpy', numpy.__version__)")
    subprocess.run([py, "-c", smoke], check=True)
    for old in os.listdir(VENV):
        if old.startswith(".ok-"):
            os.remove(os.path.join(VENV, old))
    open(stamp, "w").close()
    return py


def reexec_in_venv():
    """Raster libraries live in the venv: when started by the agent's interpreter, switch to the venv's."""
    if os.environ.get("METROLINE_FLEET_IN_VENV") == "1":
        return
    py = ensure_venv()
    env = dict(os.environ, METROLINE_FLEET_IN_VENV="1")
    os.execve(py, [py, os.path.abspath(__file__)] + sys.argv[1:], env)


# ---------------------------------------------------------------------------------------------- inputs
def load_tiles():
    return read_json(os.path.join(HERE, "ghsl_tiles_4326.json"), {"tiles": {}})["tiles"]


def tiles_for_bbox(tiles, bbox):
    s, w, n, e = bbox
    out = []
    for tid, (tw, ts, te, tn) in tiles.items():
        if w < te and e > tw and s < tn and n > ts:
            out.append(tid)
    return sorted(out)


def worldbank(force=False):
    """Employment-to-population ratio (15+) and share of population 0–14 per ISO2 country, cached a year."""
    doc = read_json(WB_FILE, None)
    if doc and not force:
        try:
            age = (datetime.now(timezone.utc) - datetime.strptime(doc["fetched"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
            if age < 365:
                return doc
        except Exception:
            pass
    out = {"fetched": now_iso(), "epr": {}, "pop0014": {}}
    for key, ind in (("epr", "SL.EMP.TOTL.SP.ZS"), ("pop0014", "SP.POP.0014.TO.ZS")):
        url = f"https://api.worldbank.org/v2/country/all/indicator/{ind}?format=json&mrnev=1&per_page=400"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.load(r)[1]
        for row in rows:
            iso2 = (row.get("country") or {}).get("id")
            v = row.get("value")
            if iso2 and v is not None:
                out[key][iso2] = float(v)
    # not covered by the World Bank
    out["epr"].setdefault("TW", 57.0); out["pop0014"].setdefault("TW", 12.0)
    out["epr"].setdefault("XK", out["epr"].get("AL", 45.0)); out["pop0014"].setdefault("XK", out["pop0014"].get("AL", 17.0))
    write_json_atomic(WB_FILE, out)
    return out


def auto_employment_total(residents_sum, country, wb):
    epr = wb["epr"].get(country, wb["epr"].get("1W", 56.0)) / 100.0          # "1W" = World aggregate id in the API
    young = wb["pop0014"].get(country, wb["pop0014"].get("1W", 25.0)) / 100.0
    return residents_sum * (1.0 - young) * epr * JOBS_C


# ---------------------------------------------------------------------------------------------- tiles
def tile_url(product, tid):
    folder, name = PRODUCTS[product]
    return f"{JRC}/{folder}/{name}/V1-0/tiles/{name}_V1_0_{tid}.zip"


def tile_path(product, tid):
    return os.path.join(TILE_CACHE, f"{PRODUCTS[product][1]}_V1_0_{tid}.zip")


def download(url, dest, deadline):
    """Resumable download (HTTP Range); returns True when the file is complete and a valid zip."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    for attempt in range(4):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                if have and r.status != 206:
                    have = 0   # server ignored the range: start over
                mode = "ab" if have else "wb"
                total = r.headers.get("Content-Range", "").split("/")[-1] or r.headers.get("Content-Length")
                with open(part, mode) as f:
                    while True:
                        if time.time() > deadline:
                            print("  download interrupted by the deadline (resumes next run)", flush=True)
                            return False
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            with zipfile.ZipFile(part) as z:
                if z.testzip() is not None:
                    raise ValueError("corrupt zip")
            os.replace(part, dest)
            print(f"  downloaded {os.path.basename(dest)} ({os.path.getsize(dest) // (1 << 20)} MB, {total} bytes announced)", flush=True)
            return True
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                print(f"  {url.split('/')[-1]}: 404 (ocean tile)", flush=True)
                return False
            if ex.code == 416:   # range not satisfiable: the part is complete or garbage
                try:
                    with zipfile.ZipFile(part) as z:
                        if z.testzip() is None:
                            os.replace(part, dest); return True
                except Exception:
                    pass
                os.remove(part)
            print(f"  {url.split('/')[-1]}: HTTP {ex.code} (attempt {attempt + 1})", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"  {url.split('/')[-1]}: {ex} (attempt {attempt + 1})", flush=True)
            if isinstance(ex, (ValueError, zipfile.BadZipFile)) and os.path.exists(part):
                os.remove(part)
        time.sleep(min(60, 10 * (attempt + 1)))
    return False


def ensure_tile(product, tid, deadline):
    """Local zip for a product tile, or None (ocean / unavailable). Also enforces the cache size."""
    dest = tile_path(product, tid)
    if os.path.exists(dest):
        os.utime(dest, None)
        return dest
    prune_tile_cache()
    return dest if download(tile_url(product, tid), dest, deadline) else None


def prune_tile_cache():
    try:
        files = [(os.path.getatime(os.path.join(TILE_CACHE, n)), os.path.join(TILE_CACHE, n)) for n in os.listdir(TILE_CACHE) if n.endswith(".zip")]
    except FileNotFoundError:
        return
    total = sum(os.path.getsize(p) for _, p in files)
    for _, p in sorted(files):
        if total <= TILE_CACHE_MAX_BYTES:
            break
        total -= os.path.getsize(p)
        os.remove(p)


def raster_member(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        tifs = [n for n in z.namelist() if n.lower().endswith(".tif")]
    if not tifs:
        raise ValueError(f"no GeoTIFF in {zip_path}")
    return f"/vsizip/{zip_path}/{tifs[0]}"


# ---------------------------------------------------------------------------------------------- bake
def bake_city(city, tiles, wb, pinned_emp, deadline):
    """grid + terrain for one city. Returns the state entry or raises."""
    import numpy as np
    import ghsl_bake
    import terrain_bake
    import rasterio
    cid, bbox, country = city["id"], city["bbox"], city.get("country")
    res_deg = RES_M / ghsl_bake.METERS_PER_DEG_LAT
    geom = ghsl_bake.target_grid_for_bbox(bbox, res_deg)
    pop = np.zeros((geom["nRows"], geom["nCols"]), dtype="float64")
    nres = np.zeros_like(pop)
    ids = tiles_for_bbox(tiles, bbox)
    if not ids:
        raise ValueError("bbox touches no GHSL land tile")
    used = 0
    with rasterio.Env(**terrain_bake.GDAL_ENV):
        for tid in ids:
            zp = ensure_tile("pop", tid, deadline)
            zn = ensure_tile("nres", tid, deadline)
            if not zp or not zn:
                if time.time() > deadline:
                    raise TimeoutError("deadline while downloading tiles")
                continue   # ocean part of the bbox
            pop += ghsl_bake.read_raster_onto_grid(raster_member(zp), geom, bbox, "nearest")
            nres += ghsl_bake.read_raster_onto_grid(raster_member(zn), geom, bbox, "nearest")
            used += 1
    if used == 0:
        raise ValueError("no GHSL tile available for the bbox")
    residents_sum = float(pop.sum())
    if residents_sum < 1000:
        raise ValueError(f"only {residents_sum:.0f} residents in the bbox — wrong bbox or empty tile")
    raw_jobs = nres / 200.0
    raw_sum = float(raw_jobs.sum())
    emp = pinned_emp.get(cid)
    emp_source = "pinned"
    if emp is None:
        emp = auto_employment_total(residents_sum, country, wb)
        emp_source = "worldbank-rule"
    residents_rows = [[float(v) for v in row] for row in pop]
    if raw_sum > 0:
        scale = emp / raw_sum
        jobs_rows = ghsl_bake.round_preserving_sum([[v * scale for v in row] for row in raw_jobs], float(emp))
    else:
        # no non-residential volume mapped: jobs follow the residents
        scale = emp / residents_sum
        jobs_rows = ghsl_bake.round_preserving_sum([[v * scale for v in row] for row in residents_rows], float(emp))
    grid = ghsl_bake.assemble_grid(cid, EPOCH, SOURCE, residents_rows, jobs_rows, geom, RES_M)
    blob = ghsl_bake.serialize_grid(grid, True)
    write_bytes_atomic(os.path.join(DATA, f"grid_{cid}.json.gz"), blob)
    summary = ghsl_bake.city_summary({"id": cid, "name": city["name"], "country": country, "bbox": bbox}, grid)
    t = terrain_bake.bake_fleet(cid, DATA, DATA, bbox)
    return {
        "generated": now_iso(), "bakeVersion": BAKE_VERSION, "tiles": ids, "residents": int(round(residents_sum)),
        "jobs": int(round(emp)), "jobsSource": emp_source, "cells": geom["nRows"] * geom["nCols"], "gridBytes": len(blob),
        "terrainBytes": t["bytes"], "waterCells": t["waterCells"], "demTiles": t["demTiles"], "worldcoverTiles": t["worldcoverTiles"],
        "catalogue": summary,
    }


def write_catalogue(pinned, cities, state):
    """cities.json: the 8 pinned entries verbatim first, then every baked fleet city (CityInfo shape)."""
    out = list(pinned)
    seen = {c["id"] for c in out}
    for c in cities:
        st = state["cities"].get(c["id"], {})
        if c["id"] in seen or not st.get("generated") or not st.get("catalogue"):
            continue
        entry = dict(st["catalogue"])
        entry["name"] = c["name"]
        out.append(entry)
    write_json_atomic(os.path.join(DATA, "cities.json"), out, compact=True)
    return len(out)


def write_summary(state, cities, note):
    cs = state["cities"]
    baked = [v for v in cs.values() if v.get("generated")]
    failed = {k: v.get("error") for k, v in cs.items() if v.get("error")}
    try:
        cache_mb = round(sum(os.path.getsize(os.path.join(TILE_CACHE, n)) for n in os.listdir(TILE_CACHE)) / 1e6)
    except FileNotFoundError:
        cache_mb = 0
    summary = {"updated": now_iso(), "total": len(cities), "baked": len(baked), "failed": len(failed), "last_run": state.get("last_run"),
               "last_note": note, "last_cities": state.get("last_cities", []), "recent_failures": dict(list(failed.items())[-10:]),
               "tile_cache_mb": cache_mb, "bake_version": BAKE_VERSION}
    write_json_atomic(SUMMARY_FILE, summary)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default=os.path.join(HERE, "cities_top1000.json"))
    ap.add_argument("--pinned", default=os.path.join(HERE, "cities.pinned.json"))
    ap.add_argument("--budget-minutes", type=float, default=50)
    ap.add_argument("--max-cities", type=int, default=60)
    ap.add_argument("--only", default=None)
    ap.add_argument("--catalogue-only", action="store_true")
    a = ap.parse_args()
    if not a.catalogue_only:
        reexec_in_venv()

    os.makedirs(DATA, exist_ok=True)
    os.makedirs(TILE_CACHE, exist_ok=True)
    cities = read_json(a.cities, {"cities": []})["cities"]
    pinned_doc = read_json(a.pinned, {"cities": []})["cities"]
    pinned = [{k: v for k, v in c.items() if k != "employmentTotal"} for c in pinned_doc]
    pinned_emp = {c["id"]: c.get("employmentTotal") for c in pinned_doc if c.get("employmentTotal")}
    pinned_ids = {c["id"] for c in pinned}
    state = read_json(STATE_FILE, {"cities": {}})
    if not isinstance(state, dict) or not isinstance(state.get("cities"), dict):
        state = {"cities": {}}

    if a.catalogue_only:
        n = write_catalogue(pinned, cities, state)
        write_summary(state, cities, f"catalogue rewritten ({n} cities)")
        print(f"catalogue: {n} cities")
        return 0

    tiles = load_tiles()
    wb = worldbank()
    deadline = time.time() + a.budget_minutes * 60
    # queue: --only, else never-baked (or baked with an older BAKE_VERSION) by rank, pinned cities excluded
    if a.only:
        want = [x.strip() for x in a.only.split(",") if x.strip()]
        known = {c["id"]: c for c in cities}
        unknown = [w for w in want if w not in known]
        if unknown:
            raise SystemExit(f"--only: unknown ids {unknown}")
        queue = [known[w] for w in want]
    else:
        def due(c):
            st = state["cities"].get(c["id"], {})
            if c["id"] in pinned_ids:
                return False
            if st.get("error") and st.get("last_attempt", "") > now_iso()[:10]:   # failed today: tomorrow
                return False
            return not st.get("generated") or st.get("bakeVersion", 0) < BAKE_VERSION
        queue = [c for c in cities if due(c)]
        # tile-first order: cities sharing the first tile of the best-ranked pending city go together
        order = {}
        for c in queue:
            first = tiles_for_bbox(tiles, c["bbox"])[:1]
            order.setdefault(first[0] if first else "-", []).append(c)
        groups = sorted(order.values(), key=lambda g: min(x.get("rank", 10**6) for x in g))
        queue = [c for g in groups for c in g][: a.max_cities]

    done, note = [], ""
    state["last_run"] = now_iso()
    for c in queue:
        if time.time() > deadline - CITY_RESERVE_S:
            note = f"budget exhausted after {len(done)} cities"; break
        st = state["cities"].setdefault(c["id"], {})
        st["last_attempt"] = now_iso()
        print(f"== {c['id']} (rank {c.get('rank')}, {c.get('name')}, {c.get('country')})", flush=True)
        t0 = time.time()
        try:
            entry = bake_city(c, tiles, wb, pinned_emp, deadline)
        except TimeoutError as e:
            st["error"] = str(e); note = f"deadline during {c['id']}"; print("  " + note, flush=True); break
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"  ERROR {st['error']}", flush=True)
            continue
        st.update(entry); st.pop("error", None)
        done.append(c["id"])
        state["last_cities"] = done[-20:]
        write_json_atomic(STATE_FILE, state)
        write_catalogue(pinned, cities, state)
        print(f"  ✓ {c['id']}: {entry['residents']} residents, {entry['jobs']} jobs ({entry['jobsSource']}), {entry['cells']} cells, "
              f"grid {entry['gridBytes'] // 1024} KB, terrain {entry['terrainBytes'] // 1024} KB, water {entry['waterCells']} cells, {time.time() - t0:.0f}s", flush=True)

    if not note:
        note = f"{len(done)} baked" if queue else "nothing due"
    write_json_atomic(STATE_FILE, state)
    n = write_catalogue(pinned, cities, state)
    s = write_summary(state, cities, note)
    print(f"run: {note}; catalogue {n} cities; baked {s['baked']}/{s['total']}; failed {s['failed']}; tile cache {s['tile_cache_mb']} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
