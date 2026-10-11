#!/usr/bin/env python3
"""publish_pinned_terrain.py — publishes the terrain of the 8 hand-baked (pinned) cities.

The GHSL fleet (ghsl_fleet.py) bakes grid + terrain for every catalogue city EXCEPT the 8 pinned launch cities, whose
calibrated grids were hand-baked and are never touched. Their terrain was baked by hand too (subwayios
tools/ghsl-bake, same geometry as the grids, the app's test fixtures) and lives here, in fleet/pinned_terrain/.

For each pinned city: the file must gunzip, decode, carry its own cityId and lie on EXACTLY the geometry of the grid the
host serves (origin, step, rows, cols, cell size; elevation and water sized rows × cols). Then it is published
atomically into the served folder when missing or different. Idempotent and stdlib-only (python 3.9): safe to run
daily. Exit 1 if any city fails a check (nothing half-written either way).

Paths (env, set by the agent): METROLINE_DATA (~/metroline-data/demand).
"""
import gzip
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
SRC = os.path.join(HERE, "pinned_terrain")
GEOMETRY = ("originLat", "originLon", "dLat", "dLon", "nRows", "nCols", "cellSizeM")


def load_gz_json(path):
    with open(path, "rb") as f:
        raw = f.read()
    return raw, json.loads(gzip.decompress(raw))


def main():
    pinned = json.load(open(os.path.join(HERE, "cities.pinned.json")))["cities"]
    ids = [c["id"] for c in pinned]
    report, failures = [], 0
    for cid in ids:
        name = f"terrain_{cid}.json.gz"
        src, dest, grid_path = os.path.join(SRC, name), os.path.join(DATA, name), os.path.join(DATA, f"grid_{cid}.json.gz")
        try:
            raw, t = load_gz_json(src)
            if t.get("cityId") != cid:
                raise ValueError(f"cityId {t.get('cityId')!r} != {cid!r}")
            n = int(t["nRows"]) * int(t["nCols"])
            if len(t["elevation"]) != n or len(t["water"]) != n:
                raise ValueError(f"layers {len(t['elevation'])}/{len(t['water'])} != {n} cells")
            _, g = load_gz_json(grid_path)
            diff = [k for k in GEOMETRY if g.get(k) != t.get(k)]
            if diff:
                raise ValueError(f"geometry differs from the served grid on {diff}")
            sha = hashlib.sha256(raw).hexdigest()[:12]
            current = open(dest, "rb").read() if os.path.exists(dest) else None
            if current == raw:
                action = "unchanged"
            else:
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.chmod(tmp, 0o644)
                os.replace(tmp, dest)
                action = "published" if current is None else "replaced"
            report.append({"city": cid, "action": action, "bytes": len(raw), "sha12": sha})
            print(f"{cid:10s} {action:9s} {len(raw):7d} B sha {sha}")
        except Exception as e:  # noqa: BLE001 — one bad city never blocks the others
            failures += 1
            report.append({"city": cid, "action": "error", "error": str(e)})
            print(f"{cid:10s} ERROR     {e}")
    print(json.dumps({"published_dir": DATA, "cities": len(ids), "failures": failures,
                      "published": sum(r["action"] in ("published", "replaced") for r in report)}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
