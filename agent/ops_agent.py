#!/usr/bin/env python3
"""
metroline-ops agent — the autonomous operator of the Metroline data host (the owner's home Mac).

Run by launchd every 10 minutes (see bootstrap.sh). Each tick:
  1. `git pull --ff-only` of this repository (the control channel: the dev side pushes jobs/commands/scripts).
  2. Runs the JOBS of jobs.json that are due (interval-based, one at a time, with a timeout and a lock).
  3. Runs each new one-shot COMMAND in commands/*.json exactly once (tracked in the local state dir).
  4. Publishes a status file the dev side reads back over HTTPS:
        ~/metroline-data/demand/_ops/status.json      (served by Caddy as /demand/_ops/status.json)
        ~/metroline-data/demand/_ops/log.txt           (tail of this agent's log)
        ~/metroline-data/demand/_ops/jobs/<job>.log    (tail of each job's output)

Guarantees: standard library only; no sudo; never touches Caddy, cloudflared, LaunchDaemons or anything outside
~/metroline-ops, ~/metroline-ops-state and ~/metroline-data/demand. Secrets are never read nor written. Any failure
is reported in status.json and the next tick retries.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import fcntl
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
REPO = os.environ.get("METROLINE_OPS_REPO", os.path.join(HOME, "metroline-ops"))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
OPS_PUB = os.path.join(DATA, "_ops")
LOG = os.path.join(STATE, "agent.log")
AGENT_VERSION = "1.0"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    line = f"{now_iso()} {msg}"
    print(line, flush=True)
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def tail(path, lines=400) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:]) + "\n"
    except Exception:
        return ""


def run(cmd, cwd=None, timeout=600, env=None):
    """Run a command, return (rc, output). Never raises."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + f"\n[timeout after {timeout}s]\n"
    except Exception as e:  # noqa: BLE001
        return 127, f"[failed to start: {e}]\n"


def git(args, timeout=120):
    return run(["git", "-C", REPO] + args, timeout=timeout)


def git_sha() -> str:
    rc, out = git(["rev-parse", "--short", "HEAD"])
    return out.strip() if rc == 0 else "unknown"


def self_update() -> dict:
    """Fast-forward the repo. On failure keep the current checkout and report."""
    before = git_sha()
    rc, out = git(["fetch", "--quiet", "origin"], timeout=180)
    if rc != 0:
        return {"ok": False, "sha": before, "error": out.strip()[-500:]}
    rc, out = git(["merge", "--ff-only", "--quiet", "origin/main"])
    after = git_sha()
    if rc != 0:
        return {"ok": False, "sha": before, "error": out.strip()[-500:]}
    if after != before:
        log(f"updated {before} → {after}")
    return {"ok": True, "sha": after, "updated": after != before}


def host_info() -> dict:
    info = {"hostname": platform.node(), "macos": platform.mac_ver()[0], "arch": platform.machine(),
            "python": sys.version.split()[0], "python_path": sys.executable, "agent": AGENT_VERSION}
    try:
        du = shutil.disk_usage(HOME)
        info["disk_free_gb"] = round(du.free / 1e9, 1)
    except Exception:
        pass
    try:
        rc, out = run(["sysctl", "-n", "kern.boottime"], timeout=5)
        if rc == 0 and "sec = " in out:
            boot = int(out.split("sec = ")[1].split(",")[0])
            info["uptime_hours"] = round((time.time() - boot) / 3600, 1)
    except Exception:
        pass
    return info


def resolve_cmd(cmd: list[str]) -> list[str]:
    """Commands reference scripts relative to the repo; `python3` resolves to the interpreter running this agent."""
    out = []
    for i, part in enumerate(cmd):
        if i == 0 and part == "python3":
            out.append(sys.executable)
        elif part.startswith("~/"):
            out.append(os.path.expanduser(part))
        else:
            out.append(part)
    return out


def job_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin")
    env["METROLINE_OPS_REPO"] = REPO
    env["METROLINE_OPS_STATE"] = STATE
    env["METROLINE_DATA"] = DATA
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_jobs(state: dict, jobs_cfg: dict, deadline: float) -> dict:
    """Run every due, enabled job once (sequentially). `state["jobs"][id]` keeps last_run/last_rc/duration."""
    jobs_state = state.setdefault("jobs", {})
    results = {}
    for job in jobs_cfg.get("jobs", []):
        jid = job["id"]
        js = jobs_state.setdefault(jid, {})
        results[jid] = js
        if not job.get("enabled", True):
            js["status"] = "disabled"
            continue
        every = float(job.get("every_minutes", 60)) * 60
        last = js.get("last_run_epoch", 0)
        if time.time() - last < every:
            js["status"] = "idle"
            js["next_run_in_min"] = round((every - (time.time() - last)) / 60, 1)
            continue
        if time.time() > deadline:
            js["status"] = "deferred (tick budget)"
            continue
        timeout = int(job.get("timeout_minutes", 55)) * 60
        log(f"job {jid}: start")
        js["status"] = "running"
        js["last_run"] = now_iso()
        js["last_run_epoch"] = time.time()
        t0 = time.time()
        rc, out = run(resolve_cmd(job["cmd"]), cwd=REPO, timeout=timeout, env=job_env())
        js["duration_s"] = round(time.time() - t0)
        js["last_rc"] = rc
        js["status"] = "ok" if rc == 0 else f"failed (rc {rc})"
        os.makedirs(os.path.join(OPS_PUB, "jobs"), exist_ok=True)
        with open(os.path.join(STATE, f"job_{jid}.log"), "a") as f:
            f.write(f"\n===== {js['last_run']} rc={rc} {js['duration_s']}s =====\n{out}")
        with open(os.path.join(OPS_PUB, "jobs", f"{jid}.log"), "w") as f:
            f.write(tail(os.path.join(STATE, f"job_{jid}.log"), 300))
        log(f"job {jid}: rc={rc} in {js['duration_s']}s")
    return results


def run_commands(state: dict) -> list:
    """One-shot commands: every commands/<name>.json is executed once, keyed by file name + its `id`/`nonce`."""
    done = state.setdefault("commands_done", {})
    history = state.setdefault("commands_history", [])
    cdir = os.path.join(REPO, "commands")
    if not os.path.isdir(cdir):
        return history[-20:]
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        spec = read_json(os.path.join(cdir, name), None)
        if not isinstance(spec, dict) or "cmd" not in spec:
            continue
        key = f"{name}#{spec.get('id', '')}#{spec.get('nonce', '')}"
        if key in done:
            continue
        timeout = int(spec.get("timeout_minutes", 30)) * 60
        log(f"command {name}: start ({spec.get('note', '')})")
        t0 = time.time()
        rc, out = run(resolve_cmd(spec["cmd"]), cwd=REPO, timeout=timeout, env=job_env())
        entry = {"name": name, "id": spec.get("id"), "note": spec.get("note"), "ran": now_iso(), "rc": rc,
                 "duration_s": round(time.time() - t0), "output_tail": out[-4000:]}
        done[key] = entry["ran"]
        history.append(entry)
        del history[:-50]
        log(f"command {name}: rc={rc} in {entry['duration_s']}s")
    return history[-20:]


def publish(status: dict) -> None:
    os.makedirs(OPS_PUB, exist_ok=True)
    write_json_atomic(os.path.join(OPS_PUB, "status.json"), status)
    with open(os.path.join(OPS_PUB, "log.txt"), "w") as f:
        f.write(tail(LOG, 400))


def main() -> int:
    os.makedirs(STATE, exist_ok=True)
    lock_path = os.path.join(STATE, "agent.lock")
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another tick is running — exit", flush=True)
        return 0
    tick_started = time.time()
    deadline = tick_started + 50 * 60   # a tick never outlives its launchd interval by much
    state_path = os.path.join(STATE, "state.json")
    state = read_json(state_path, {})
    state["tick_started"] = now_iso()
    update = self_update()
    jobs_cfg = read_json(os.path.join(REPO, "jobs.json"), {"jobs": []})
    status = {
        "agent": AGENT_VERSION, "tick": state["tick_started"], "repo_sha": update.get("sha"), "repo_update": update,
        "host": host_info(), "jobs": {}, "commands": [], "fleet": read_json(os.path.join(STATE, "poi_fleet_summary.json"), None),
    }
    publish(status)   # early heartbeat even if a job then runs for 50 minutes
    try:
        status["commands"] = run_commands(state)
        write_json_atomic(state_path, state)
        status["jobs"] = run_jobs(state, jobs_cfg, deadline)
    except Exception as e:  # noqa: BLE001
        log(f"tick error: {e!r}")
        status["error"] = repr(e)
    state["tick_finished"] = now_iso()
    write_json_atomic(state_path, state)
    status["fleet"] = read_json(os.path.join(STATE, "poi_fleet_summary.json"), None)
    status["tick_finished"] = state["tick_finished"]
    status["tick_duration_s"] = round(time.time() - tick_started)
    publish(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
