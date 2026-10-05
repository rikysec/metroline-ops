#!/usr/bin/env python3
"""
metroline-ops agent — the autonomous operator of the Metroline data host (the owner's home Mac).

Started by launchd every 10 minutes through agent/launcher.py (installed OUTSIDE the repo by bootstrap.sh, so a bad
push can never take the control channel down: the launcher updates the checkout, compiles this file and falls back to
the last good commit). Each tick:
  1. Runs the JOBS of jobs.json that are due (interval-based, one at a time, with a timeout and a lock), publishing a
     heartbeat while a long job runs.
  2. Runs each new one-shot COMMAND in commands/*.json exactly once (at-most-once: recorded before it starts).
  3. Publishes a status file the dev side reads back over HTTPS:
        ~/metroline-data/demand/_ops/status.json      (served by Caddy as /demand/_ops/status.json)
        ~/metroline-data/demand/_ops/log.txt           (tail of this agent's log)
        ~/metroline-data/demand/_ops/jobs/<job>.log    (tail of each job's output)

Guarantees: standard library only; no sudo; never touches Caddy, cloudflared, LaunchDaemons or anything outside
~/metroline-ops, ~/metroline-ops-state and ~/metroline-data/demand. Secrets are never read nor written. Any failure
is reported in status.json and the next tick retries.
"""
from __future__ import annotations

import fcntl
import json
import select
import signal
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
REPO = os.environ.get("METROLINE_OPS_REPO", os.path.join(HOME, "metroline-ops"))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
DATA = os.environ.get("METROLINE_DATA", os.path.join(HOME, "metroline-data", "demand"))
OPS_PUB = os.path.join(DATA, "_ops")
LOG = os.path.join(STATE, "agent.log")
AGENT_VERSION = "1.1"
LOG_ROTATE_BYTES = 5 * 1024 * 1024
HEARTBEAT_S = 300


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


def write_text_atomic(path: str, text: str) -> None:
    """tmp in the state dir + os.replace: a served file is never half-written (same volume: both under $HOME)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    tmp = os.path.join(STATE, ".pub." + os.path.basename(path) + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path, obj) -> None:
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


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


def rotate_logs() -> None:
    """One generation at 5 MB: agent.log, job_*.log and launchd's own files stay bounded (≈ 10 MB each at most)."""
    try:
        for name in os.listdir(STATE):
            if not name.endswith(".log"):
                continue
            p = os.path.join(STATE, name)
            if os.path.getsize(p) > LOG_ROTATE_BYTES:
                os.replace(p, p + ".1")
    except Exception:
        pass


def job_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    env["METROLINE_OPS_REPO"] = REPO
    env["METROLINE_OPS_STATE"] = STATE
    env["METROLINE_DATA"] = DATA
    env["PYTHONUNBUFFERED"] = "1"
    return env


def resolve_cmd(cmd) -> list:
    """Commands reference scripts relative to the repo; `python3` resolves to the interpreter running this agent."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise ValueError("cmd must be a non-empty list of strings")
    out = []
    for i, part in enumerate(cmd):
        if i == 0 and part == "python3":
            out.append(sys.executable)
        elif part.startswith("~/"):
            out.append(os.path.expanduser(part))
        else:
            out.append(part)
    return out


def run(cmd, timeout=600, log_path=None, heartbeat=None):
    """Run a command with the repo as cwd; stream its output to `log_path`; call `heartbeat()` every 5 minutes while
    it runs. Returns (rc, output_tail). Never raises (124 = timeout, 127 = could not start)."""
    buf = bytearray()
    lf = None
    try:
        argv = resolve_cmd(cmd)
        lf = open(log_path, "ab") if log_path else None
        # own session: a timeout kills the whole process group (sh -c … sleep … would otherwise outlive the job)
        p = subprocess.Popen(argv, cwd=REPO, env=job_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        fd = p.stdout.fileno()
        started = time.time()
        last_beat = started
        timed_out = False

        def drain(wait):
            r, _, _ = select.select([fd], [], [], wait)
            if not r:
                return True
            chunk = os.read(fd, 65536)
            if not chunk:
                return False   # EOF
            buf.extend(chunk)
            if len(buf) > 128 * 1024:
                del buf[:-64 * 1024]
            if lf:
                lf.write(chunk); lf.flush()
            return True

        while True:
            alive = drain(0.5)
            if not alive and p.poll() is not None:
                break
            if p.poll() is not None and not alive:
                break
            if not timed_out and time.time() - started > timeout:
                timed_out = True
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()
                note = f"\n[timeout after {timeout}s]\n".encode()
                buf.extend(note)
                if lf:
                    lf.write(note)
            if timed_out and p.poll() is not None:
                while drain(0.2):
                    if p.stdout.closed:
                        break
                    if not select.select([fd], [], [], 0)[0]:
                        break
                break
            if heartbeat and time.time() - last_beat > HEARTBEAT_S:
                last_beat = time.time()
                try:
                    heartbeat()
                except Exception:
                    pass
        try:
            p.wait(10)
        except Exception:
            pass
        rc = 124 if timed_out else p.returncode
        return rc, bytes(buf[-8000:]).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 127, (bytes(buf[-4000:]).decode("utf-8", "replace") + f"\n[failed to start: {e}]\n")
    finally:
        if lf:
            lf.close()


def git_sha() -> str:
    git = os.environ.get("METROLINE_GIT") or shutil.which("git", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin") or "git"
    try:
        p = subprocess.run([git, "-C", REPO, "rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def host_info() -> dict:
    info = {"hostname": platform.node(), "macos": platform.mac_ver()[0], "arch": platform.machine(),
            "python": sys.version.split()[0], "python_bin": os.path.basename(os.path.dirname(os.path.dirname(sys.executable))) + "/…/" + os.path.basename(sys.executable),
            "agent": AGENT_VERSION, "launch": os.environ.get("METROLINE_LAUNCH", "unknown")}
    try:
        du = shutil.disk_usage(HOME)
        info["disk_free_gb"] = round(du.free / 1e9, 1)
        total = 0
        for root, _, files in os.walk(STATE):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        info["state_dir_mb"] = round(total / 1e6, 1)
    except Exception:
        pass
    try:
        p = subprocess.run(["sysctl", "-n", "kern.boottime"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        if p.returncode == 0 and "sec = " in p.stdout:
            boot = int(p.stdout.split("sec = ")[1].split(",")[0])
            info["uptime_hours"] = round((time.time() - boot) / 3600, 1)
    except Exception:
        pass
    return info


class Status:
    """The published status; `publish()` can be called at any time (heartbeat)."""

    def __init__(self, launcher: dict):
        self.doc = {"agent": AGENT_VERSION, "tick": now_iso(), "repo_sha": git_sha(), "launcher": launcher,
                    "host": host_info(), "jobs": {}, "commands": [], "errors": [],
                    "fleet": read_json(os.path.join(STATE, "poi_fleet_summary.json"), None),
                    "health": read_json(os.path.join(STATE, "health.json"), None)}

    def publish(self):
        self.doc["heartbeat"] = now_iso()
        self.doc["fleet"] = read_json(os.path.join(STATE, "poi_fleet_summary.json"), None)
        self.doc["health"] = read_json(os.path.join(STATE, "health.json"), None)
        os.makedirs(OPS_PUB, exist_ok=True)
        write_json_atomic(os.path.join(OPS_PUB, "status.json"), self.doc)
        write_text_atomic(os.path.join(OPS_PUB, "log.txt"), tail(LOG, 400))


def run_jobs(state: dict, jobs_cfg, status: Status, deadline: float) -> None:
    """Run every due, enabled job once (sequentially). `state["jobs"][id]` keeps last_run/last_rc/duration."""
    jobs_state = state.setdefault("jobs", {})
    status.doc["jobs"] = jobs_state
    if not isinstance(jobs_cfg, dict) or not isinstance(jobs_cfg.get("jobs"), list):
        status.doc["errors"].append("jobs.json unreadable or malformed — keeping the previous job states, nothing run")
        return
    for job in jobs_cfg["jobs"]:
        try:
            jid = str(job["id"])
            js = jobs_state.setdefault(jid, {})
            js["note"] = job.get("note")
            if not job.get("enabled", True):
                js["status"] = "disabled"
                continue
            every = max(1.0, float(job.get("every_minutes", 60))) * 60
            last = float(js.get("last_run_epoch", 0))
            if time.time() - last < every:
                js["status"] = "idle"
                js["next_run_in_min"] = round((every - (time.time() - last)) / 60, 1)
                continue
            if time.time() > deadline:
                js["status"] = "deferred (tick budget)"
                continue
            timeout = int(float(job.get("timeout_minutes", 55)) * 60)
            argv = resolve_cmd(job["cmd"])   # validates
            log(f"job {jid}: start {' '.join(argv[1:]) if argv[0] == sys.executable else ' '.join(argv)}")
            js["status"] = "running"
            js["last_run"] = now_iso()
            js["last_run_epoch"] = time.time()
            js.pop("next_run_in_min", None)
            status.publish()
            t0 = time.time()
            job_log = os.path.join(STATE, f"job_{jid}.log")
            with open(job_log, "a") as f:
                f.write(f"\n===== {js['last_run']} start =====\n")
            rc, _ = run(job["cmd"], timeout=timeout, log_path=job_log, heartbeat=status.publish)
            js["duration_s"] = round(time.time() - t0)
            js["last_rc"] = rc
            js["status"] = "ok" if rc == 0 else f"failed (rc {rc})"
            write_text_atomic(os.path.join(OPS_PUB, "jobs", f"{jid}.log"), tail(job_log, 300))
            log(f"job {jid}: rc={rc} in {js['duration_s']}s")
        except Exception as e:  # noqa: BLE001  — one malformed job never blocks the others
            msg = f"job {job.get('id', '?')}: configuration error {e!r}"
            log(msg)
            status.doc["errors"].append(msg)
            if isinstance(job, dict) and "id" in job:
                jobs_state.setdefault(str(job["id"]), {})["status"] = f"config error: {e}"[:200]
        finally:
            status.publish()


def run_commands(state: dict, status: Status, state_path: str) -> None:
    """One-shot commands: every commands/<name>.json runs once, keyed by file name + id + nonce, and is recorded
    as done BEFORE it starts (at-most-once even if the tick dies)."""
    done = state.setdefault("commands_done", {})
    history = state.setdefault("commands_history", [])
    status.doc["commands"] = history[-20:]
    cdir = os.path.join(REPO, "commands")
    if not os.path.isdir(cdir):
        return
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cdir, name)
        try:
            with open(path) as f:
                spec = json.load(f)
        except Exception as e:  # noqa: BLE001
            key = f"{name}#invalid#{int(os.path.getmtime(path))}"
            if key not in done:
                done[key] = now_iso()
                history.append({"name": name, "ran": now_iso(), "rc": -1, "output_tail": f"invalid JSON: {e}"})
                status.doc["errors"].append(f"command {name}: invalid JSON")
            continue
        if not isinstance(spec, dict) or "cmd" not in spec:
            continue
        key = f"{name}#{spec.get('id', '')}#{spec.get('nonce', '')}"
        if key in done:
            continue
        done[key] = now_iso()
        write_json_atomic(state_path, state)   # recorded first: a killed tick does not re-run the command
        try:
            timeout = int(float(spec.get("timeout_minutes", 30)) * 60)
            log(f"command {name}: start ({spec.get('note', '')})")
            t0 = time.time()
            rc, out = run(spec["cmd"], timeout=timeout, log_path=os.path.join(STATE, "job_commands.log"), heartbeat=status.publish)
            entry = {"name": name, "id": spec.get("id"), "note": spec.get("note"), "ran": now_iso(), "rc": rc,
                     "duration_s": round(time.time() - t0), "output_tail": out[-4000:]}
            log(f"command {name}: rc={rc} in {entry['duration_s']}s")
        except Exception as e:  # noqa: BLE001
            entry = {"name": name, "id": spec.get("id"), "ran": now_iso(), "rc": -1, "output_tail": f"error: {e!r}"}
            status.doc["errors"].append(f"command {name}: {e!r}")
        history.append(entry)
        del history[:-50]
        status.doc["commands"] = history[-20:]
        write_json_atomic(state_path, state)
        status.publish()


def main() -> int:
    os.makedirs(STATE, exist_ok=True)
    rotate_logs()
    lock = open(os.path.join(STATE, "agent.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another tick is running — exit", flush=True)
        return 0
    tick_started = time.time()
    deadline = tick_started + 50 * 60   # a tick never outlives its launchd interval by much
    state_path = os.path.join(STATE, "state.json")
    state = read_json(state_path, {})
    if not isinstance(state, dict):
        log("state.json was not an object — starting a fresh state (previous one kept as state.json.bad)")
        try:
            os.replace(state_path, state_path + ".bad")
        except OSError:
            pass
        state = {}
    state["tick_started"] = now_iso()
    launcher = read_json(os.path.join(STATE, "launcher.json"), {})
    status = Status(launcher)
    status.publish()   # early heartbeat even if a job then runs for 50 minutes
    jobs_cfg = read_json(os.path.join(REPO, "jobs.json"), None)
    try:
        run_commands(state, status, state_path)
        write_json_atomic(state_path, state)
        run_jobs(state, jobs_cfg, status, deadline)
    except Exception as e:  # noqa: BLE001
        log(f"tick error: {e!r}")
        status.doc["errors"].append(f"tick error: {e!r}")
    state["tick_finished"] = now_iso()
    write_json_atomic(state_path, state)
    status.doc["tick_finished"] = state["tick_finished"]
    status.doc["tick_duration_s"] = round(time.time() - tick_started)
    status.publish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
