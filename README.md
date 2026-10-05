# metroline-ops — the autonomous operator of the Metroline data host

The Metroline app downloads its demand data (GHSL grids, terrain, points of interest) from
`https://data.metroline.app/demand/…`, a folder served by Caddy on the owner's home Mac through a Cloudflare Tunnel.
This repository is **the control channel of that Mac**: an unprivileged agent on the Mac pulls it every 10 minutes,
runs the jobs it declares, executes one-shot commands, and publishes its status back over HTTPS. Nobody needs to
log in, paste commands or move files by hand: the dev side (Riccardo, or the Claude session that develops the app)
**pushes a commit**, the Mac **does it**, and the result is readable at

```
https://data.metroline.app/demand/_ops/status.json      (add ?t=<epoch> to skip the CDN cache)
https://data.metroline.app/demand/_ops/log.txt
https://data.metroline.app/demand/_ops/jobs/poi_fleet.log
```

Only scripts, a city list and job tables live here — no secrets, no logs with visitor data — so the repository can
be public and the Mac needs no credentials to read it.

## What runs on the Mac

| Piece | Where | What |
|---|---|---|
| `agent/launcher.py` | `~/metroline-ops-state/launcher.py` (copied by bootstrap, **outside** the repo), launchd `app.metroline.ops` every 600 s | fetch + fast-forward (hard reset when upstream history was rewritten and the mirror is clean), byte-compile check with rollback to the last good commit, then runs the agent — a bad push can never brick the channel |
| `agent/ops_agent.py` | `~/metroline-ops` | due jobs (heartbeat every 5 min while one runs), new commands (at-most-once), `status.json` |
| `jobs.json` | repo | recurring jobs: `poi_fleet` (every 12 h, ≤35 cities, 50-minute budget), `health` (every 10 min) |
| `commands/*.json` | repo | one-shot commands, run once each (see `commands/README.md`) |
| `fleet/poi_fleet.py` | repo | bakes `poi_<city>.json.gz` for `fleet/cities_top1000.json`, writes `poi_cities.json` (the app's catalogue) |
| `fleet/poi_bake.py` | repo | the bake itself (Overpass + Wikidata, curation, `poi_overrides.json`) — mirror of `subwayios/tools/ghsl-bake/poi_bake.py` |
| `~/metroline-ops-state/` | Mac | state, logs, Overpass cache (14 days), never served |
| `~/metroline-data/demand/` | Mac (served) | the files + `_ops/` status |

The agent writes only under `~/metroline-ops`, `~/metroline-ops-state`, `~/metroline-data/demand` and its own plist in
`~/Library/LaunchAgents`. It never touches Caddy, the tunnels, LaunchDaemons or anything that needs `sudo`.

## Install on the Mac (once, no password)

In a Terminal of the logged-in user on the data host:

```bash
curl -fsSL https://raw.githubusercontent.com/rikysec/metroline-ops/main/bootstrap.sh | bash
```

It checks `python3`/`git` (without triggering the Command Line Tools installer), clones this repo, installs the
LaunchAgent, runs the first tick and prints the status. Re-running is safe. Because it is a per-user LaunchAgent it
runs **while that user has a login session** (screen locked is fine; logged out or sitting at the login window after
a reboot is not). To survive reboots without a login, run once with `sudo` the daemon variant — the only step that
asks for the owner's password:

```bash
curl -fsSL https://raw.githubusercontent.com/rikysec/metroline-ops/main/bootstrap.sh | sudo -E bash -s -- --daemon
```

## How the dev side operates it

* **Change a schedule / budget** → edit `jobs.json`, commit, push. Picked up within 10 minutes.
* **Run something now** (rebake a city after an override edit, rewrite the catalogue, …) → add
  `commands/<name>.json` with a new `nonce`, commit, push. Result in `status.json → commands`.
* **Fix a bug in the bake** → edit `fleet/*.py` here (and keep `subwayios/tools/ghsl-bake/poi_bake.py` identical),
  commit, push: the next tick runs the new code.
* **Add/adjust a hub** → `fleet/poi_overrides.json` + a command `--only <city>`.
* **Watch** → `curl -s "https://data.metroline.app/demand/_ops/status.json?t=$(date +%s)" | python3 -m json.tool`.
  `fleet.baked / fleet.total`, `fleet.last_note`, `jobs.poi_fleet.status`, `host.disk_free_gb`, `errors`,
  `launcher.update/compile`, and `heartbeat` (refreshed every 5 minutes even during a long job: older than 30
  minutes means the Mac is asleep, logged out or offline).
* **Trust boundary** → push access to this repository is code execution as the Mac's user every 10 minutes
  (the agent deliberately runs whatever `jobs.json`/`commands/` say). Keep the GitHub account behind 2FA; the agent
  itself has no sudo and no secrets to leak, and the served folder is append-only data.

## The POI fleet

`fleet/cities_top1000.json` (GeoNames cities15000, CC BY 4.0: the 1000 most populous places after merging entries
within 20 km; the 8 GHSL cities keep their ids and bboxes) is baked in rank order, ≤35 cities per run, two runs a day,
every city refreshed every 90 days (≈ 11 cities/day at steady state). One Overpass query per city (`out tags bb qt`,
status-poll before each query, 60 s between cities, back-off on 429/504 — the public instances' fair use is ~100
queries/day for an application), then Wikidata SPARQL by POST in batches of 200. Output: `poi_<id>.json.gz` (3–15 KB)
and `poi_cities.json` with a `generated` stamp per city; the app requests `poi_<id>.json.gz?v=<generated>`, so every
new bake is a new CDN cache entry and nothing has to be purged.

Known calibration caveat: footprints come from OSM bounds (0.55 × box for ways, 0.40 for multipolygon relations,
measured on the 8 reference cities), capped per type; in East-Asian megacities hundreds of large "plaza" malls hit
the 40 000/day cap each (Shenzhen: 211 malls ≈ 3.1 M visitors/day), a share of the city's PT trips that is still small
but worth a per-category sanity pass in P5. Relations spanning more than 1.5 km (multi-site campuses, hospital
trusts) never anchor a POI on their own.

Fair-use note: the OSM wiki asks heavy or commercial users to self-host Overpass. The fleet stays well inside the
"does not disturb" envelope; if the daily volume ever has to grow (hourly refresh, more categories), the plan is a
self-hosted Overpass with regional extracts on the same Mac rather than more load on the public mirrors.

## Files the Mac publishes

```
demand/poi_cities.json                 catalogue (compact JSON, ~200 KB): id, name, names, country, lat, lon, radiusKm,
                                       population, rank, generated (null = not baked yet), count, visitorsPerDay
demand/poi_<id>.json.gz                one per baked city (schemaVersion 1, see subwayios/Subway/PoiLayer.swift)
demand/_ops/status.json                agent heartbeat: tick, repo_sha, host, jobs, commands, fleet summary
demand/_ops/log.txt, _ops/jobs/*.log   tails
```
