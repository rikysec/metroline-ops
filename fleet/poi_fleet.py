#!/usr/bin/env python3
"""
poi_fleet.py — bakes the POI layer (poi_<city>.json.gz) of the world's most populous cities, a few per run, and keeps
the catalogue the app reads (poi_cities.json) up to date. Designed to be called by the ops agent twice a day with a
time budget; it is resumable, idempotent and polite to Overpass/Wikidata.

    python3 fleet/poi_fleet.py [--budget-minutes 50] [--max-cities 35] [--refresh-days 90] [--only id,id]

Order of work: never-baked cities by rank first, then the ones whose bake is older than --refresh-days (oldest first).
After each city the file is moved atomically into the served folder and poi_cities.json is regenerated, so the app
sees every city as soon as it is done. A busy Overpass (429/503/504/timeouts on every attempt) or a refused Wikidata
User-Agent ends the run early: the next run tries again. The run never schedules work past its deadline (the bake
reads `poi_bake.DEADLINE`), so the agent's timeout never kills it mid-city. Standard library only.

Paths (overridable by env, set by the agent): METROLINE_OPS_REPO (this repo), METROLINE_OPS_STATE (~/metroline-ops-state),
METROLINE_DATA (~/metroline-data/demand — the folder Caddy serves as /demand/).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import poi_bake  # noqa: E402

HOME = os.path.expanduser("~")
REPO = os.environ.get("METROLINE_OPS_REPO", os.path.dirname(HERE))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
WORK = os.path.join(STATE, "poi_work")
STATE_FILE = os.path.join(STATE, "poi_fleet.json")
SUMMARY_FILE = os.path.join(STATE, "poi_fleet_summary.json")
CATALOGUE_SCHEMA = 1
OSM_CACHE_DAYS = 14          # raw Overpass answers are kept this long (rebake after an override edit is free)
SLEEP_BETWEEN_CITIES_S = 60  # Overpass dispatcher penalty is capped at 60 s: a fixed floor between queries keeps the slot clean
CITY_RESERVE_S = 12 * 60     # a city is not started with less than this left: one worst-case Overpass round


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


def load_cities(path):
    doc = read_json(path, None)
    if not doc or "cities" not in doc:
        sys.exit(f"city list not readable: {path}")
    return doc["cities"]


def days_since(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
    except Exception:
        return 10**6


def write_catalogue(cities, state):
    """poi_cities.json — what the app downloads first: every city of the fleet with its bake stamp (null = not yet)."""
    out = []
    for c in cities:
        st = state.get("cities", {}).get(c["id"], {})
        out.append({
            "id": c["id"], "name": c["name"], "names": c.get("names", [c["name"]]), "country": c.get("country"),
            "lat": c["lat"], "lon": c["lon"], "radiusKm": c.get("radiusKm"), "population": c.get("population"),
            "rank": c.get("rank"), "generated": st.get("generated"), "count": st.get("count"),
            "visitorsPerDay": st.get("visitorsPerDay"),
        })
    baked = sum(1 for e in out if e["generated"])
    doc = {"schemaVersion": CATALOGUE_SCHEMA, "updated": now_iso(), "baked": baked, "total": len(out),
           "source": "GeoNames cities15000 (CC BY 4.0); OpenStreetMap contributors (ODbL); Wikidata (CC0)",
           "cities": out}
    write_json_atomic(os.path.join(DATA, "poi_cities.json"), doc, compact=True)
    return baked


def write_summary(state, cities, note):
    cs = state.get("cities", {})
    baked = [v for v in cs.values() if v.get("generated")]
    failed = {k: v.get("error") for k, v in cs.items() if v.get("error")}
    oldest = min((v["generated"] for v in baked), default=None)
    summary = {
        "updated": now_iso(), "total": len(cities), "baked": len(baked), "failed": len(failed),
        "oldest_bake": oldest, "last_run": state.get("last_run"), "last_note": note,
        "last_cities": state.get("last_cities", []), "recent_failures": dict(list(failed.items())[-10:]),
    }
    write_json_atomic(SUMMARY_FILE, summary)
    return summary


def prune_osm_cache():
    try:
        cutoff = time.time() - OSM_CACHE_DAYS * 86400
        for name in os.listdir(WORK):
            p = os.path.join(WORK, name)
            if (name.endswith(".osm.json") and os.path.getmtime(p) < cutoff) or name.endswith(".tmp"):
                os.remove(p)
    except FileNotFoundError:
        pass


def adopt_served_files(cities, state):
    """Files already in the served folder but unknown to the state (the first 8 cities copied by hand) count as baked."""
    for c in cities:
        p = os.path.join(DATA, f"poi_{c['id']}.json.gz")
        st = state["cities"].setdefault(c["id"], {})
        if st.get("generated") or not os.path.exists(p):
            continue
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                doc = json.load(f)
            gen = str(doc.get("generated") or date.today())
            if "T" not in gen:
                gen += "T00:00:00Z"
            st.update(generated=gen, count=len(doc.get("pois", [])),
                      visitorsPerDay=sum(x.get("visitorsPerDay", 0) for x in doc.get("pois", [])), adopted=True)
        except Exception as e:  # noqa: BLE001
            st["error"] = f"unreadable served file: {e}"


def build_queue(cities, state, only, refresh_days, max_cities):
    """--only = exactly those ids (unknown ids are an error); else never-baked by rank, then stale (oldest first)."""
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        known = {c["id"]: c for c in cities}
        unknown = [w for w in want if w not in known]
        if unknown:
            raise SystemExit(f"--only: unknown city ids {unknown}")
        return [known[w] for w in want]
    cs = state["cities"]
    never = [c for c in cities if not cs.get(c["id"], {}).get("generated")]
    # a city that failed is retried after the others, at most once a day
    never.sort(key=lambda c: (1 if cs.get(c["id"], {}).get("error") and days_since(cs[c["id"]].get("last_attempt", "")) < 1 else 0,
                              c.get("rank", 10**6)))
    stale = sorted((c for c in cities if cs.get(c["id"], {}).get("generated") and days_since(cs[c["id"]]["generated"]) >= refresh_days),
                   key=lambda c: cs[c["id"]]["generated"])
    return (never + stale)[:max_cities]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default=os.path.join(HERE, "cities_top1000.json"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "poi_overrides.json"))
    ap.add_argument("--budget-minutes", type=float, default=50)
    ap.add_argument("--max-cities", type=int, default=35)
    ap.add_argument("--refresh-days", type=int, default=90)
    ap.add_argument("--only", default=None, help="comma-separated ids to (re)bake now, ignoring the schedule")
    ap.add_argument("--catalogue-only", action="store_true", help="just rewrite poi_cities.json and exit")
    a = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    cities = load_cities(a.cities)
    overrides = read_json(a.overrides, [])
    state = read_json(STATE_FILE, {"cities": {}})
    if not isinstance(state, dict) or not isinstance(state.get("cities"), dict):
        state = {"cities": {}}
    adopt_served_files(cities, state)

    if a.catalogue_only:
        n = write_catalogue(cities, state)
        write_json_atomic(STATE_FILE, state)
        write_summary(state, cities, f"catalogue rewritten ({n} baked)")
        print(f"catalogue: {n}/{len(cities)} baked")
        return 0

    queue = build_queue(cities, state, a.only, a.refresh_days, a.max_cities)
    deadline = time.time() + a.budget_minutes * 60
    poi_bake.DEADLINE = deadline + 5 * 60   # in-flight retries may spill a little past the budget, never past the agent's timeout
    done, note = [], ""
    state["last_run"] = now_iso()
    touched_network = False
    for c in queue:
        if touched_network:
            time.sleep(SLEEP_BETWEEN_CITIES_S)   # the inter-city floor applies whatever the previous outcome was
            touched_network = False
        if time.time() > deadline - CITY_RESERVE_S:
            note = f"budget exhausted after {len(done)} cities"; break
        st = state["cities"].setdefault(c["id"], {})
        st["last_attempt"] = now_iso()
        cache = os.path.join(WORK, f"poi_{c['id']}.osm.json")
        if a.only and os.path.exists(cache):
            os.remove(cache)   # a forced rebake must not reuse a stale Overpass answer
        touched_network = not os.path.exists(cache)
        print(f"== {c['id']} (rank {c.get('rank')}, {c.get('name')}, {c.get('country')})", flush=True)
        try:
            r = poi_bake.bake({"id": c["id"], "bbox": c["bbox"]}, WORK, overrides)
        except poi_bake.OverpassBusy as e:
            st["error"] = str(e)[:300]
            note = f"overpass busy at {c['id']} — stopping this run"
            print("  " + note, flush=True)
            break
        except poi_bake.WikidataRefused as e:
            st["error"] = str(e)[:300]
            note = f"wikidata refused the user agent at {c['id']} — stopping this run (fix the UA, then rerun)"
            print("  " + note, flush=True)
            break
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"  ERROR {st['error']}", flush=True)
            if isinstance(e, (ValueError, OSError)) and os.path.exists(cache):
                os.remove(cache)   # a corrupt cached answer must not fail the city for 14 days
            continue
        if not r:
            st["error"] = "bake produced nothing"
            continue
        src = os.path.join(WORK, f"poi_{c['id']}.json.gz")
        dst = os.path.join(DATA, f"poi_{c['id']}.json.gz")
        os.replace(src, dst)   # same volume (both under $HOME): an atomic rename, never a half-written served file
        st.update(generated=now_iso(), count=r["count"], visitorsPerDay=r["visitorsPerDay"], osmElements=r["osmElements"],
                  dropped=r["dropped"], bytes=os.path.getsize(dst))
        st.pop("error", None)
        st.pop("adopted", None)
        done.append(c["id"])
        state["last_cities"] = done[-20:]
        write_json_atomic(STATE_FILE, state)
        write_catalogue(cities, state)

    if not note:
        note = f"{len(done)} baked" if queue else "nothing due"
    write_json_atomic(STATE_FILE, state)
    baked = write_catalogue(cities, state)
    prune_osm_cache()
    s = write_summary(state, cities, note)
    print(f"run: {note}; catalogue {baked}/{len(cities)} baked; failed {s['failed']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
