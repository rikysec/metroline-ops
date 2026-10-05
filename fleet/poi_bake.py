#!/usr/bin/env python3
"""poi_bake.py — curated points of interest per city, baked once and served next to the demand grid.

For every city in cities.config.json this writes poi_<id>.json.gz: the attractors that generate trips of their
own on top of the residents + jobs of the GHSL grid (travellers at a station, fans at a stadium, visitors at a
mall, students on a campus, patients and visitors at a hospital). Each POI carries ONE number the engine uses,
`visitorsPerDay` (people entering it on an average day), its weekly profile and a display tier.

Sources: OpenStreetMap via Overpass (geometry + tags) and Wikidata via SPARQL (passengers/day P1373,
passengers/year P3872, visitors/year P1174, capacity P1083, platforms P1103, students P2196, beds P6801), plus
a small hand-kept override file (poi_overrides.json) for the hubs Wikidata leaves blank (Torino Porta Susa…).

CURATION (the point of baking instead of reading OSM live — see POI_REVIEW_2026-10-05.md):
  • one POI per institution: elements sharing a Wikidata id, or the same normalised institution name within
    400 m, are merged (58 "amenity=university" nodes in Torino → the universities, not their offices);
  • rail stations: visitors from Wikidata or the override; otherwise a small default (3 000/day) — never
    "platforms × 5 000", which ranked a suburban stop above Porta Susa;
  • airports only with an IATA code (an airfield is not an airport);
  • stadiums only with capacity ≥ 10 000; malls only with a known visitor count or a footprint ≥ 10 000 m²;
  • hospitals with beds (OSM/Wikidata) or a footprint ≥ 5 000 m²; conference centres with a capacity;
  • everything left over (a department, a clinic, a small arena) is dropped: it is already in the jobs layer.

Usage:  python3 poi_bake.py --out out_poi [--cities torino,milano]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
WIKIDATA = "https://query.wikidata.org/sparql"
# Wikimedia User-Agent policy: client name + version + contact URL + library — never a bare/browser UA (that lands in
# the anonymous-scraper rate tier or gets blocked). The same string is sent to Overpass.
UA = "MetrolinePOIBot/1.1 (+https://metroline.app) python-urllib/3"
WIKIDATA_BATCH = 200   # QIDs per VALUES block (measured 0.9 s; hard max 500 — a GET of 500 already returns HTTP 431)

PROFILE = {"stadium": "eventPeak", "conference": "eventPeak", "mall": "weekend", "university": "weekday",
           "rail_station": "steady", "airport": "steady", "hospital": "steady"}
# Daily visitors from a size metric when no count exists (same spirit as the old client formula, calmer).
DEFAULT_VISITORS = {"rail_station": 3000, "hospital": 2000, "conference": 1500, "university": 4000}
# Ceilings for FOOTPRINT-based estimates: a campus or hospital drawn as a 25 ha landuse polygon is not 25 ha of
# building (Agraria at Grugliasco would have scored 64 000/day); real counts and overrides are never capped.
MIN_VISITORS = 500
AREA_CAP = {"university": 12_000, "hospital": 10_000, "mall": 40_000}


def http_json(url, data=None, timeout=180, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def retry_after_s(ex, default):
    """Honour a Retry-After header (delta-seconds) on 429/503, else the given default."""
    try:
        v = ex.headers.get("Retry-After") if getattr(ex, "headers", None) else None
        return max(default, float(v)) if v and v.strip().isdigit() else default
    except Exception:
        return default


# Wall-clock limit set by the fleet (epoch seconds): no retry or sleep may be scheduled past it, so a run
# ends before the agent's timeout kills it mid-write. None = no limit (manual runs).
DEADLINE = None


def time_left():
    return float("inf") if DEADLINE is None else DEADLINE - time.time()


class OverpassBusy(Exception):
    """Overpass refused the query for load reasons (429 rate-limited / 504 dispatcher busy / timeouts) on every
    attempt, or the run's deadline leaves no room for another attempt: the caller should stop the run and come
    back later, not hammer the mirror."""


class WikidataRefused(Exception):
    """Wikidata answered 403: the User-Agent is blocked. Stop the run; retrying would make it worse."""


OVERPASS_STATUS = "https://overpass-api.de/api/status"
_overpass_dead = set()   # mirrors that timed out in this process: not tried again this run


def overpass_wait_for_slot(max_polls=5, max_wait=180):
    """Overpass fair use: ask /api/status for a free slot before each query (no Retry-After is ever sent). Returns
    the seconds waited (capped). Any failure to read the status is ignored (the query itself will tell)."""
    waited = 0.0
    for _ in range(max_polls):
        try:
            req = urllib.request.Request(OVERPASS_STATUS, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = r.read().decode("utf-8", "replace")
        except Exception:
            return waited
        m = re.search(r"(\d+) slots? available now", txt)
        if m and int(m.group(1)) >= 1:
            return waited
        waits = [int(x) for x in re.findall(r"in (\d+) seconds", txt)]
        pause = (min(waits) + 2) if waits else 20
        pause = min(max(pause, 2), 120)
        if waited + pause > max_wait or pause > time_left():
            return waited
        time.sleep(pause)
        waited += pause
    return waited


def overpass(bbox, retries=3):
    """One request per city: tags + bounds only ("out tags bb qt"). Measured on the exact selection: the runtime is
    the bbox selection, not the output, and geometry doubles the bytes for nothing (the footprint estimate below
    uses the bounds). [timeout:60][maxsize:128 MiB]: the dispatcher admits a query only if its declared budget is
    at most half of what is free, so the defaults (180 s / 512 MiB) get 504 under load. Returns the element list
    or raises OverpassBusy (never returns None)."""
    s, w, n, e = bbox[0], bbox[1], bbox[2], bbox[3]
    bb = f"({s},{w},{n},{e})"
    big = (n - s) * (e - w) > 0.2
    q = f"""[out:json][timeout:{90 if big else 60}][maxsize:{268435456 if big else 134217728}];(
nwr["aeroway"="aerodrome"]["name"]{bb};
nwr["railway"="station"]["name"]{bb};
nwr["leisure"="stadium"]["name"]{bb};
nwr["amenity"~"^(university|hospital|conference_centre)$"]["name"]{bb};
nwr["shop"="mall"]["name"]{bb};
);out tags bb qt;"""
    body = ("data=" + urllib.parse.quote(q)).encode()
    load_errors = 0
    for attempt in range(retries):
        overpass_wait_for_slot()
        for ep in OVERPASS:
            if ep in _overpass_dead:
                continue
            primary = ep == OVERPASS[0]
            # client timeout = declared query timeout + transfer margin on the primary; a mirror that hangs is
            # given 60 s once and then skipped for the rest of the process
            timeout = (130 if big else 100) if primary else 60
            if timeout > time_left():
                raise OverpassBusy("run deadline reached before the next Overpass attempt")
            try:
                return http_json(ep, data=body, timeout=timeout)["elements"]
            except urllib.error.HTTPError as ex:  # pragma: no cover
                sys.stderr.write(f"  overpass {ep} attempt {attempt + 1}: HTTP {ex.code}\n")
                if ex.code == 400:
                    raise RuntimeError("Overpass rejected the query (400) — a bug, not load")
                if ex.code in (429, 503, 504):
                    load_errors += 1
                elif not primary:
                    _overpass_dead.add(ep)
            except Exception as ex:  # pragma: no cover
                sys.stderr.write(f"  overpass {ep} attempt {attempt + 1}: {ex}\n")
                load_errors += 1   # a read timeout is a load symptom too
                if not primary:
                    _overpass_dead.add(ep)
        if attempt < retries - 1:
            pause = 30 * (attempt + 1)
            if pause > time_left():
                raise OverpassBusy("run deadline reached while backing off from Overpass")
            time.sleep(pause)
    raise OverpassBusy(f"Overpass unavailable after {retries} attempts ({load_errors} load errors)")


def wikidata(ids):
    """Size signals of the OSM elements' Wikidata items, one POST per batch, strictly serial. GROUP BY ?item with
    MAX()/SAMPLE() so multi-valued properties do not cross-multiply the rows. A 5xx halves the batch and retries
    the SAME ids after the back-off; ids advance only after a definitive answer or give-up for that chunk."""
    out = {}
    ids = sorted(set(ids))
    batch = WIKIDATA_BATCH
    i = 0
    consecutive_failures = 0
    while i < len(ids):
        chunk = ids[i:i + batch]
        q = ("SELECT ?item (MAX(?daily_) AS ?daily) (MAX(?annual_) AS ?annual) (MAX(?visitors_) AS ?visitors) "
             "(MAX(?capacity_) AS ?capacity) (MAX(?platforms_) AS ?platforms) (MAX(?students_) AS ?students) "
             "(MAX(?beds_) AS ?beds) (SAMPLE(?iata_) AS ?iata) WHERE { VALUES ?item { "
             + " ".join("wd:" + x for x in chunk) + " } "
             "OPTIONAL{?item wdt:P1373 ?daily_.} OPTIONAL{?item wdt:P3872 ?annual_.} OPTIONAL{?item wdt:P1174 ?visitors_.} "
             "OPTIONAL{?item wdt:P1083 ?capacity_.} OPTIONAL{?item wdt:P1103 ?platforms_.} OPTIONAL{?item wdt:P2196 ?students_.} "
             "OPTIONAL{?item wdt:P6801 ?beds_.} OPTIONAL{?item wdt:P238 ?iata_.} } GROUP BY ?item")
        rows = None
        halved = False
        for attempt in range(5):
            if 70 > time_left():
                sys.stderr.write("  wikidata: run deadline reached — the remaining items keep their OSM-only sizes\n")
                return out
            try:
                rows = http_json(WIKIDATA, data=("query=" + urllib.parse.quote(q)).encode(), timeout=70,
                                 headers={"Accept": "application/sparql-results+json",
                                          "Content-Type": "application/x-www-form-urlencoded"})["results"]["bindings"]
                break
            except urllib.error.HTTPError as ex:  # pragma: no cover
                sys.stderr.write(f"  wikidata attempt {attempt + 1}: HTTP {ex.code}\n")
                if ex.code == 403:
                    raise WikidataRefused("Wikidata refused the User-Agent (403) — stop, do not retry")
                pause = retry_after_s(ex, 5 * 2 ** attempt)
                if ex.code in (500, 502, 503, 504) and batch > 50:
                    batch //= 2
                    sys.stderr.write(f"  wikidata batch → {batch}\n")
                    halved = True
                    if pause <= time_left():
                        time.sleep(pause)
                    break   # rebuild the query with the smaller chunk, same i
                if attempt < 4 and pause <= time_left():
                    time.sleep(pause)
            except Exception as ex:  # pragma: no cover
                sys.stderr.write(f"  wikidata attempt {attempt + 1}: {ex}\n")
                pause = 5 * 2 ** attempt
                if attempt < 4 and pause <= time_left():
                    time.sleep(pause)
        if halved and rows is None:
            continue   # retry the same ids with the halved batch
        if rows is None:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                sys.stderr.write("  wikidata: 3 consecutive failures — giving up on the rest (sizes from OSM only)\n")
                break
            i += len(chunk)
            continue
        consecutive_failures = 0
        for b in rows:
            qid = b["item"]["value"].rsplit("/", 1)[1]
            d = out.setdefault(qid, {})
            for k in ("daily", "annual", "visitors", "capacity", "platforms", "students", "beds"):
                if k in b and b[k].get("value") not in (None, ""):
                    try:
                        d[k] = max(d.get(k, 0.0), float(b[k]["value"]))
                    except ValueError:
                        pass
            if "iata" in b and b["iata"].get("value"):
                d["iata"] = b["iata"]["value"]
        i += len(chunk)
        if i < len(ids):
            time.sleep(1.5)
    return out


def classify(t):
    if t.get("aeroway") == "aerodrome": return "airport"
    if t.get("railway") == "station" and t.get("station") not in ("subway", "light_rail", "monorail", "funicular"): return "rail_station"
    if t.get("leisure") == "stadium": return "stadium"
    if t.get("amenity") == "university": return "university"
    if t.get("amenity") == "hospital": return "hospital"
    if t.get("shop") == "mall": return "mall"
    if t.get("amenity") == "conference_centre": return "conference"
    return None


def ring_area_m2(coords):
    if len(coords) < 3:
        return 0.0
    lat0 = sum(c[0] for c in coords) / len(coords)
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 111_320
    pts = [(c[1] * kx, c[0] * ky) for c in coords]
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def meters(a, b):
    R = 6_371_000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(h)))


def fold(name):
    """Script-agnostic folding: NFKC + casefold, Latin diacritics stripped, everything that is not a word character
    dropped — so 'Barcelona - Sants' == 'Barcelona-Sants' and a Chinese or Arabic name keeps its characters (the old
    ASCII-only fold made every non-Latin name empty and merged unrelated places)."""
    n = unicodedata.normalize("NFKC", name or "").casefold()
    n = "".join(ch for ch in unicodedata.normalize("NFKD", n) if not unicodedata.combining(ch))
    return n


def institution_key(name):
    """'Università di Torino - Dipartimento di Fisica' → 'universita di torino' (the part before a separator)."""
    n = fold(name)
    n = re.split(r"\s[-–—:(]\s?|\s[-–—:(]|,", n)[0]
    n = re.sub(r"\b(dipartimento|facolta|sede|campus|aule?|segreteria|biblioteca|laboratorio|ufficio|polo|edificio|padiglione)\b.*", "", n)
    n = re.sub(r"[\W_]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def norm_name(name):
    """Override / duplicate key: folded, word characters only."""
    return re.sub(r"[\W_]+", "", fold(name))


def num(t, *keys):
    for k in keys:
        v = t.get(k)
        if v:
            try:
                return float(str(v).replace(",", ".").split()[0])
            except ValueError:
                pass
    return None


def bake(city, out_dir, overrides):
    cid, bbox = city["id"], city["bbox"]
    cache = os.path.join(out_dir, f"poi_{cid}.osm.json")
    els = None
    if os.path.exists(cache):
        try:
            els = json.load(open(cache)); print(f"  OSM: {len(els)} elements (cached)")
        except (ValueError, OSError) as ex:
            print(f"  OSM cache unreadable ({ex}) — refetching")
            os.remove(cache)
    if els is None:
        els = overpass(bbox)   # raises OverpassBusy when the mirrors are saturated
        os.makedirs(out_dir, exist_ok=True)
        with open(cache + ".tmp", "w") as f:
            json.dump(els, f)
        os.replace(cache + ".tmp", cache)
        print(f"  OSM: {len(els)} elements")

    raws = []
    for e in els:
        t = e.get("tags", {}); ty = classify(t)
        if not ty:
            continue
        lat = e.get("lat") or (e.get("center") or {}).get("lat"); lon = e.get("lon") or (e.get("center") or {}).get("lon")
        geom = e.get("geometry")
        area = ring_area_m2([(p["lat"], p["lon"]) for p in geom]) if e["type"] == "way" and geom else 0.0
        if e["type"] == "relation" and geom:
            # legacy "out center geom" answer: footprint = Σ outer rings; position = centroid of the LARGEST outer ring
            rings = [[(p["lat"], p["lon"]) for p in m["geometry"]] for m in e.get("members", [])
                     if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")]
            areas = [ring_area_m2(r) for r in rings]
            if rings:
                area = sum(areas)
                big = rings[max(range(len(rings)), key=lambda i: areas[i])]
                lat = sum(c[0] for c in big) / len(big); lon = sum(c[1] for c in big) / len(big)
        b = e.get("bounds")
        if b and (lat is None or lon is None):
            lat = (b["minlat"] + b["maxlat"]) / 2; lon = (b["minlon"] + b["maxlon"]) / 2
        if b and not area:
            # "out tags bb": footprint from the bounding rectangle × the polygon/box ratio measured on the 8 cities'
            # geometry answers (1 105 ways: median 0.54; 172 multipolygon relations: median 0.44, mean 0.40 — a
            # campus relation's box spans several buildings).
            dlat = (b["maxlat"] - b["minlat"]) * 111_320.0
            dlon = (b["maxlon"] - b["minlon"]) * 111_320.0 * math.cos(math.radians((b["minlat"] + b["maxlat"]) / 2))
            area = (0.40 if e["type"] == "relation" else 0.55) * dlat * dlon
        if (lat is None or lon is None) and geom:
            lat = sum(p["lat"] for p in geom) / len(geom); lon = sum(p["lon"] for p in geom) / len(geom)
        if lat is None or lon is None:
            continue
        # A relation whose box spans more than 1.5 km (a university with several sites, a hospital trust) has no
        # single door: it may only ENRICH a group anchored by one of its buildings, never anchor one itself.
        multisite = False
        if e["type"] == "relation" and b:
            diag = meters((b["minlat"], b["minlon"]), (b["maxlat"], b["maxlon"]))
            multisite = diag > 1500
        raws.append(dict(id=f"{e['type']}/{e['id']}", type=ty, name=t.get("name", ty), lat=lat, lon=lon,
                         wd=t.get("wikidata"), iata=t.get("iata"), area=area, multisite=multisite,
                         capacity=num(t, "capacity", "seats"), beds=num(t, "beds", "hospital:beds")))

    wd = wikidata([r["wd"] for r in raws if r["wd"] and re.match(r"^Q\d+$", r["wd"])])
    print(f"  Wikidata: {len(wd)} items with data")

    # --- merge elements of the same institution (same wikidata id, or same key within 400 m) ---
    groups = []
    dropped_multisite = 0
    for r in sorted(raws, key=lambda r: (1 if r.get("multisite") else 0, -(r["area"] or 0), r["id"])):
        key = institution_key(r["name"]) if r["type"] in ("university", "hospital") else None
        nn = norm_name(r["name"])
        placed = False
        for g in groups:
            if g["type"] != r["type"]:
                continue
            same_wd = r["wd"] and g["wd"] and r["wd"] == g["wd"]
            # a multi-site relation joins by identity (wikidata) or by name anywhere inside its own box
            close = r.get("multisite") or meters((r["lat"], r["lon"]), (g["lat"], g["lon"])) < 400
            same_key = bool(key) and g["key"] == key and close
            # the same place mapped twice (node + area, or way + relation): same name within 400 m
            same_name = bool(nn) and nn == norm_name(g["name"]) and close
            if same_wd or same_key or same_name:
                g["members"].append(r)
                if not r.get("multisite"):
                    g["area"] = max(g["area"], r["area"] or 0)
                if not g["wd"] and r["wd"]: g["wd"] = r["wd"]
                if not g["capacity"] and r["capacity"]: g["capacity"] = r["capacity"]
                if not g["beds"] and r["beds"]: g["beds"] = r["beds"]
                placed = True; break
        if not placed:
            if r.get("multisite"):
                dropped_multisite += 1
                continue
            groups.append(dict(type=r["type"], key=key, wd=r["wd"], lat=r["lat"], lon=r["lon"], area=r["area"] or 0,
                               name=r["name"], id=r["id"], iata=r["iata"], capacity=r["capacity"], beds=r["beds"], members=[r]))
    if dropped_multisite:
        print(f"  multi-site relations without a building of their own: {dropped_multisite} dropped")

    pois, dropped = [], {}
    ov_by_wd = {o["wikidata"]: o for o in overrides if o.get("wikidata")}
    ov_by_name = {}
    for o in overrides:
        for n in [o.get("name")] + o.get("aliases", []):
            if n: ov_by_name[(o["city"], norm_name(n))] = o
    for g in groups:
        ty = g["type"]; w = wd.get(g["wd"] or "", {})
        name = g["name"]
        # the group takes the name of its member with the shortest institution name (the institution itself)
        if ty in ("university", "hospital") and len(g["members"]) > 1:
            name = min((m["name"] for m in g["members"]), key=lambda n: (len(institution_key(n)), len(n)))
            g["lat"] = sum(m["lat"] for m in g["members"]) / len(g["members"])
            g["lon"] = sum(m["lon"] for m in g["members"]) / len(g["members"])
        ov = ov_by_wd.get(g["wd"]) or ov_by_name.get((cid, norm_name(name))) \
            or next((ov_by_name[(cid, norm_name(m["name"]))] for m in g["members"] if (cid, norm_name(m["name"])) in ov_by_name), None)
        visitors, src = None, None
        if ov:
            visitors, src = float(ov["visitorsPerDay"]), "override"
        elif w.get("daily"): visitors, src = w["daily"], "P1373"
        elif w.get("annual"): visitors, src = w["annual"] / 365, "P3872"
        elif w.get("visitors"): visitors, src = w["visitors"] / 365, "P1174"
        if visitors is None:
            if ty == "airport":
                if not (g["iata"] or w.get("iata")):
                    dropped[ty] = dropped.get(ty, 0) + 1; continue
                visitors, src = 8000.0, "default airport (IATA, no count)"
            elif ty == "rail_station":
                visitors, src = float(DEFAULT_VISITORS[ty]), "default"
            elif ty == "stadium":
                cap = w.get("capacity") or g["capacity"]
                if not cap or cap < 10_000:
                    dropped[ty] = dropped.get(ty, 0) + 1; continue
                visitors, src = cap * 0.3, "capacity"
            elif ty == "conference":
                cap = w.get("capacity") or g["capacity"]
                visitors, src = (cap * 0.3, "capacity") if cap else (float(DEFAULT_VISITORS[ty]), "default")
            elif ty == "university":
                if w.get("students"): visitors, src = w["students"] * 0.7, "students"
                elif g["area"] >= 5_000: visitors, src = g["area"] * 0.25, f"area {g['area']:.0f} m2"
                elif len(g["members"]) >= 3: visitors, src = float(DEFAULT_VISITORS[ty]), "default (multi-site)"
                else:
                    dropped[ty] = dropped.get(ty, 0) + 1; continue
            elif ty == "hospital":
                beds = w.get("beds") or g["beds"]
                if beds: visitors, src = beds * 6, "beds"
                elif g["area"] >= 5_000: visitors, src = g["area"] * 0.12, f"area {g['area']:.0f} m2"
                else:
                    dropped[ty] = dropped.get(ty, 0) + 1; continue
            elif ty == "mall":
                if g["area"] >= 10_000: visitors, src = g["area"] * 0.6, f"area {g['area']:.0f} m2"
                else:
                    dropped[ty] = dropped.get(ty, 0) + 1; continue
        if src.startswith("area") and ty in AREA_CAP:
            visitors = min(visitors, AREA_CAP[ty])
        visitors = int(round(visitors))
        # Noise floor: a place with fewer than MIN_VISITORS people a day (a heliport with a yearly count in the
        # hundreds, a tiny clinic) is not an attractor for a metro.
        if visitors < MIN_VISITORS:
            dropped[ty] = dropped.get(ty, 0) + 1; continue
        tier = "L" if visitors >= 50_000 else ("M" if visitors >= 12_000 else "S")
        pois.append(dict(id=g["id"], type=ty, name=(ov.get("name") if ov else None) or name, lat=round(g["lat"], 6), lon=round(g["lon"], 6),
                         visitorsPerDay=visitors, profile=PROFILE[ty], tier=tier, wikidata=g["wd"], source=src,
                         merged=len(g["members"]), _ov=(id(ov) if ov else None)))
    # Two groups that resolved to the SAME override are the same place mapped twice (node + area far apart,
    # "Paddington" + "London Paddington"): keep the first, drop the rest.
    seen_ov, deduped = set(), []
    for p in pois:
        key = p.pop("_ov", None)
        if key is not None:
            if key in seen_ov:
                continue
            seen_ov.add(key)
        deduped.append(p)
    pois = deduped
    pois.sort(key=lambda p: -p["visitorsPerDay"])
    out = dict(cityId=cid, generated=str(date.today()), schemaVersion=1,
               source="OpenStreetMap (Overpass) + Wikidata; curated by tools/ghsl-bake/poi_bake.py", pois=pois)
    path = os.path.join(out_dir, f"poi_{cid}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    by_type = {}
    for p in pois:
        by_type.setdefault(p["type"], [0, 0]); by_type[p["type"]][0] += 1; by_type[p["type"]][1] += p["visitorsPerDay"]
    print(f"  → {path}: {len(pois)} POI (dropped {dropped}) size={os.path.getsize(path)//1024} KB")
    for ty, (n, v) in sorted(by_type.items(), key=lambda kv: -kv[1][1]):
        print(f"     {ty:13s} n={n:3d} visitors/day={v:8d}")
    for p in pois[:8]:
        print(f"     {p['visitorsPerDay']:7d} {p['tier']} {p['type']:12s} {p['name'][:40]:40s} [{p['source']}]")
    gaps = [p for p in pois if p["type"] == "rail_station" and p["source"] == "default"]
    if gaps:
        print(f"     stations without a count (default {DEFAULT_VISITORS['rail_station']}/day): " + ", ".join(p["name"] for p in gaps[:12]))
    return dict(cityId=cid, path=path, count=len(pois), visitorsPerDay=sum(p["visitorsPerDay"] for p in pois),
                dropped=dropped, generated=out["generated"], osmElements=len(els))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(HERE, "cities.config.json"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "poi_overrides.json"))
    ap.add_argument("--cities", default=None)
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    overrides = json.load(open(a.overrides)) if os.path.exists(a.overrides) else []
    want = set(a.cities.split(",")) if a.cities else None
    for c in cfg["cities"]:
        if want and c["id"] not in want:
            continue
        print(f"== {c['id']}", flush=True)
        bake(c, a.out, overrides)
        time.sleep(3)


if __name__ == "__main__":
    main()
