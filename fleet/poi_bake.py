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
UA = "metroline-poi-bake/1.0 (https://metroline.app)"

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


class OverpassBusy(Exception):
    """Every Overpass endpoint answered 429/504 on every attempt: the caller should back off, not retry at once."""


def overpass(bbox, retries=3):
    s, w, n, e = bbox[0], bbox[1], bbox[2], bbox[3]
    bb = f"({s},{w},{n},{e})"
    q = f"""[out:json][timeout:120];(
nwr["aeroway"="aerodrome"]["name"]{bb};
nwr["railway"="station"]["name"]{bb};
nwr["leisure"="stadium"]["name"]{bb};
nwr["amenity"="university"]["name"]{bb};
nwr["amenity"="hospital"]["name"]{bb};
nwr["shop"="mall"]["name"]{bb};
nwr["amenity"="conference_centre"]["name"]{bb};
);out center geom;"""   # NOT "out tags": that verbosity drops relation members/centers
    busy_only = True
    for attempt in range(retries):
        for ep in OVERPASS:
            try:
                return http_json(ep, data=("data=" + urllib.parse.quote(q)).encode())["elements"]
            except urllib.error.HTTPError as ex:  # pragma: no cover
                sys.stderr.write(f"  overpass {ep} attempt {attempt + 1}: HTTP {ex.code}\n")
                if ex.code not in (429, 504, 503):
                    busy_only = False
            except Exception as ex:  # pragma: no cover
                sys.stderr.write(f"  overpass {ep} attempt {attempt + 1}: {ex}\n")
                busy_only = False
        time.sleep(20 * (attempt + 1))
    if busy_only:
        raise OverpassBusy("all Overpass endpoints busy (429/503/504)")
    return None


def wikidata(ids):
    out = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), 150):
        chunk = ids[i:i + 150]
        q = ("SELECT ?item ?daily ?annual ?visitors ?capacity ?platforms ?students ?beds ?iata WHERE { VALUES ?item { "
             + " ".join("wd:" + x for x in chunk) + " } "
             "OPTIONAL{?item wdt:P1373 ?daily.} OPTIONAL{?item wdt:P3872 ?annual.} OPTIONAL{?item wdt:P1174 ?visitors.} "
             "OPTIONAL{?item wdt:P1083 ?capacity.} OPTIONAL{?item wdt:P1103 ?platforms.} OPTIONAL{?item wdt:P2196 ?students.} "
             "OPTIONAL{?item wdt:P6801 ?beds.} OPTIONAL{?item wdt:P238 ?iata.} }")
        for attempt in range(3):
            try:
                rows = http_json(WIKIDATA + "?format=json&query=" + urllib.parse.quote(q),
                                 headers={"Accept": "application/sparql-results+json"})["results"]["bindings"]
                break
            except Exception as ex:  # pragma: no cover
                sys.stderr.write(f"  wikidata attempt {attempt + 1}: {ex}\n"); rows = []; time.sleep(10)
        for b in rows:
            qid = b["item"]["value"].rsplit("/", 1)[1]
            d = out.setdefault(qid, {})
            for k in ("daily", "annual", "visitors", "capacity", "platforms", "students", "beds"):
                if k in b:
                    try:
                        d[k] = max(d.get(k, 0.0), float(b[k]["value"]))
                    except ValueError:
                        pass
            if "iata" in b:
                d["iata"] = b["iata"]["value"]
        time.sleep(1)
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


def institution_key(name):
    """'Università di Torino - Dipartimento di Fisica' → 'universita di torino' (the part before a separator)."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.split(r"\s[-–—:(]\s?|\s[-–—:(]|,", n)[0]
    n = re.sub(r"\b(dipartimento|facolta|sede|campus|aule?|segreteria|biblioteca|laboratorio|ufficio|polo|edificio|padiglione)\b.*", "", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def norm_name(name):
    """Override / duplicate key: lower-case, no accents, letters and digits only ('Barcelona - Sants' == 'Barcelona-Sants')."""
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", n)


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
    if os.path.exists(cache):
        els = json.load(open(cache)); print(f"  OSM: {len(els)} elements (cached)")
    else:
        els = overpass(bbox)
        if els is None:
            print("  WARNING: Overpass failed — skipped"); return None
        os.makedirs(out_dir, exist_ok=True); json.dump(els, open(cache, "w"))
        print(f"  OSM: {len(els)} elements")

    raws = []
    for e in els:
        t = e.get("tags", {}); ty = classify(t)
        if not ty:
            continue
        lat = e.get("lat") or (e.get("center") or {}).get("lat"); lon = e.get("lon") or (e.get("center") or {}).get("lon")
        geom = e.get("geometry")
        area = ring_area_m2([(p["lat"], p["lon"]) for p in geom]) if e["type"] == "way" and geom else 0.0
        if e["type"] == "relation":
            # multipolygon: footprint = Σ outer rings; position = centroid of the LARGEST outer ring (a university
            # relation spanning several sites must sit on its main campus, not on the mean of all sites)
            rings = [[(p["lat"], p["lon"]) for p in m["geometry"]] for m in e.get("members", [])
                     if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")]
            areas = [ring_area_m2(r) for r in rings]
            if rings:
                area = sum(areas)
                big = rings[max(range(len(rings)), key=lambda i: areas[i])]
                lat = sum(c[0] for c in big) / len(big); lon = sum(c[1] for c in big) / len(big)
        if (lat is None or lon is None) and geom:
            lat = sum(p["lat"] for p in geom) / len(geom); lon = sum(p["lon"] for p in geom) / len(geom)
        if lat is None or lon is None:
            continue
        raws.append(dict(id=f"{e['type']}/{e['id']}", type=ty, name=t.get("name", ty), lat=lat, lon=lon,
                         wd=t.get("wikidata"), iata=t.get("iata"), area=area,
                         capacity=num(t, "capacity", "seats"), beds=num(t, "beds", "hospital:beds")))

    wd = wikidata([r["wd"] for r in raws if r["wd"] and re.match(r"^Q\d+$", r["wd"])])
    print(f"  Wikidata: {len(wd)} items with data")

    # --- merge elements of the same institution (same wikidata id, or same key within 400 m) ---
    groups = []
    for r in sorted(raws, key=lambda r: (-(r["area"] or 0), r["id"])):
        key = institution_key(r["name"]) if r["type"] in ("university", "hospital") else None
        placed = False
        for g in groups:
            if g["type"] != r["type"]:
                continue
            same_wd = r["wd"] and g["wd"] and r["wd"] == g["wd"]
            close = meters((r["lat"], r["lon"]), (g["lat"], g["lon"])) < 400
            same_key = key and g["key"] == key and close
            # the same place mapped twice (node + area, or way + relation): same name within 400 m
            same_name = norm_name(r["name"]) == norm_name(g["name"]) and close
            if same_wd or same_key or same_name:
                g["members"].append(r); g["area"] = max(g["area"], r["area"] or 0)
                if not g["wd"] and r["wd"]: g["wd"] = r["wd"]
                placed = True; break
        if not placed:
            groups.append(dict(type=r["type"], key=key, wd=r["wd"], lat=r["lat"], lon=r["lon"], area=r["area"] or 0,
                               name=r["name"], id=r["id"], iata=r["iata"], capacity=r["capacity"], beds=r["beds"], members=[r]))

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
