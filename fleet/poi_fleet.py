#!/usr/bin/env python3
"""
poi_fleet.py — bakes the POI layer (poi_<city>.json.gz) of the world's most populous cities, a few per run, and keeps
the catalogue the app reads (poi_cities.json) up to date. Designed to be called by the ops agent every hour with a
time budget; it is resumable, idempotent and polite to Overpass/Wikidata.

    python3 fleet/poi_fleet.py [--budget-minutes 50] [--max-cities 12] [--refresh-days 90] [--only id,id]

Order of work: never-baked cities by rank first, then the ones whose bake is older than --refresh-days (oldest first).
After each city the file is moved atomically into the served folder and poi_cities.json is regenerated, so the app
sees every city as soon as it is done. A busy Overpass (429/503/504 on every endpoint) ends the run early: the next
hourly run tries again. Standard library only.

Paths (overridable by env, set by the agent): METROLINE_OPS_REPO (this repo), METROLINE_OPS_STATE (~/metroline-ops-state),
METROLINE_DATA (~/metroline-data/demand — the folder Caddy serves as /demand/).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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
SLEEP_BETWEEN_CITIES_S = 60  # Overpass dispatcher penalty is capped at 60 s: a fixed floor between cities keeps the slot clean


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
            if name.endswith(".osm.json") and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except FileNotFoundError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default=os.path.join(HERE, "cities_top1000.json"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "poi_overrides.json"))
    ap.add_argument("--budget-minutes", type=float, default=50)
    ap.add_argument("--max-cities", type=int, default=12)
    ap.add_argument("--refresh-days", type=int, default=90)
    ap.add_argument("--only", default=None, help="comma-separated ids to (re)bake now, ignoring the schedule")
    ap.add_argument("--catalogue-only", action="store_true", help="just rewrite poi_cities.json and exit")
    a = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    cities = load_cities(a.cities)
    overrides = read_json(a.overrides, [])
    state = read_json(STATE_FILE, {"cities": {}})
    state.setdefault("cities", {})

    # Adopt files that are already served but unknown to the state (e.g. the first 8 cities copied by hand).
    for c in cities:
        p = os.path.join(DATA, f"poi_{c['id']}.json.gz")
        st = state["cities"].setdefault(c["id"], {})
        if not st.get("generated") and os.path.exists(p):
            try:
                import gzip
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    doc = json.load(f)
                gen = str(doc.get("generated") or date.today())
                if "T" not in gen:
                    gen += "T00:00:00Z"
                st.update(generated=gen, count=len(doc.get("pois", [])),
                          visitorsPerDay=sum(x.get("visitorsPerDay", 0) for x in doc.get("pois", [])), adopted=True)
            except Exception as e:  # noqa: BLE001
                st["error"] = f"unreadable served file: {e}"

    if a.catalogue_only:
        n = write_catalogue(cities, state)
        write_json_atomic(STATE_FILE, state)
        write_summary(state, cities, f"catalogue rewritten ({n} baked)")
        print(f"catalogue: {n}/{len(cities)} baked")
        return 0

    # Queue: explicit --only, else never-baked by rank, then stale (oldest first).
    if a.only:
        want = [x.strip() for x in a.only.split(",") if x.strip()]
        queue = [c for c in cities if c["id"] in want]
    else:
        never = [c for c in cities if not state["cities"].get(c["id"], {}).get("generated")]
        stale = sorted((c for c in cities if state["cities"].get(c["id"], {}).get("generated")
                        and days_since(state["cities"][c["id"]]["generated"]) >= a.refresh_days),
                       key=lambda c: state["cities"][c["id"]]["generated"])
        # a city that failed is retried after the others, at most once a day
        never.sort(key=lambda c: (1 if state["cities"].get(c["id"], {}).get("error") and
                                  days_since(state["cities"][c["id"]].get("last_attempt", "")) < 1 else 0, c.get("rank", 10**6)))
        queue = never + stale
    queue = queue[: a.max_cities]

    deadline = time.time() + a.budget_minutes * 60
    done, note = [], ""
    state["last_run"] = now_iso()
    for i, c in enumerate(queue):
        if time.time() > deadline:
            note = "budget exhausted"; break
        st = state["cities"].setdefault(c["id"], {})
        st["last_attempt"] = now_iso()
        print(f"== {c['id']} (rank {c.get('rank')}, {c.get('name')}, {c.get('country')})", flush=True)
        try:
            # a forced rebake must not reuse a stale Overpass answer
            if a.only:
                cache = os.path.join(WORK, f"poi_{c['id']}.osm.json")
                if os.path.exists(cache):
                    os.remove(cache)
            r = poi_bake.bake({"id": c["id"], "bbox": c["bbox"]}, WORK, overrides)
        except poi_bake.OverpassBusy as e:
            st["error"] = str(e)
            note = f"overpass busy at {c['id']} — stopping this run"
            print("  " + note, flush=True)
            break
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"  ERROR {st['error']}", flush=True)
            continue
        if not r:
            st["error"] = "overpass failed"
            continue
        src = os.path.join(WORK, f"poi_{c['id']}.json.gz")
        dst = os.path.join(DATA, f"poi_{c['id']}.json.gz")
        shutil.copyfile(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        os.remove(src)
        st.update(generated=now_iso(), count=r["count"], visitorsPerDay=r["visitorsPerDay"], osmElements=r["osmElements"],
                  dropped=r["dropped"], bytes=os.path.getsize(dst))
        st.pop("error", None)
        st.pop("adopted", None)
        done.append(c["id"])
        state["last_cities"] = done[-20:]
        write_json_atomic(STATE_FILE, state)
        write_catalogue(cities, state)
        if i < len(queue) - 1:
            time.sleep(SLEEP_BETWEEN_CITIES_S)

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
