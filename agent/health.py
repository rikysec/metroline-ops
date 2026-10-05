#!/usr/bin/env python3
"""health.py — cheap local checks, published inside status.json by the agent (as the `health` job output)."""
import json, os, shutil, sys, urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
out = {"checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
try:
    with urllib.request.urlopen("http://127.0.0.1:8095/demand/cities.json", timeout=5) as r:
        out["caddy_local"] = r.status
except Exception as e:  # noqa: BLE001
    out["caddy_local"] = f"error: {e}"
try:
    names = os.listdir(DATA)
    out["files"] = {"grid": sum(n.startswith("grid_") for n in names), "terrain": sum(n.startswith("terrain_") for n in names),
                    "poi": sum(n.startswith("poi_") and n.endswith(".json.gz") for n in names),
                    "catalogue": "poi_cities.json" in names, "cities_json": "cities.json" in names}
    out["served_mb"] = round(sum(os.path.getsize(os.path.join(DATA, n)) for n in names if os.path.isfile(os.path.join(DATA, n))) / 1e6, 1)
except Exception as e:  # noqa: BLE001
    out["files"] = f"error: {e}"
du = shutil.disk_usage(HOME)
out["disk_free_gb"] = round(du.free / 1e9, 1)
os.makedirs(STATE, exist_ok=True)
with open(os.path.join(STATE, "health.json"), "w") as f:
    json.dump(out, f, indent=2)
print(json.dumps(out, indent=2))
sys.exit(0 if isinstance(out.get("caddy_local"), int) and out["caddy_local"] == 200 else 1)
