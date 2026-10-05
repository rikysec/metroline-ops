# One-shot commands

Drop a `<name>.json` here, commit, push: the server agent runs it **once** (keyed by file name + `id` + `nonce`) within
10 minutes and reports rc, duration and the output tail under `commands` in `status.json`. To run the same command
again, change the `nonce`.

```json
{ "id": "rebake-torino", "nonce": "2026-10-05a", "note": "override edited: Porta Susa 65k",
  "cmd": ["python3", "fleet/poi_fleet.py", "--only", "torino,milano", "--budget-minutes", "20"],
  "timeout_minutes": 30 }
```

`cmd` runs with the repo as working directory and the agent's Python as `python3`. Keep commands inside the repo's own
scripts: the agent is deliberately unprivileged (no sudo, no access to Caddy, tunnels or LaunchDaemons).
