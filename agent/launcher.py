#!/usr/bin/env python3
"""
launcher.py — the launchd entry point, installed by bootstrap.sh OUTSIDE the repo (~/metroline-ops-state/launcher.py)
and never self-updated, so a bad push can never take the control channel down.

Each tick: fetch origin and fast-forward the checkout (a pure mirror: when ff is impossible — force-push, rebase —
and the tree is clean, hard-reset to origin/main); byte-compile the agent; if the new revision does not compile, go
back to the last known-good commit; then run the agent. Whatever happens, write launcher.json (the agent publishes
it) so the failure mode is visible from outside. Standard library only. Copy of agent/launcher.py in the repo.
"""
import json
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
REPO = os.environ.get("METROLINE_OPS_REPO", os.path.join(HOME, "metroline-ops"))
STATE = os.environ.get("METROLINE_OPS_STATE", os.path.join(HOME, "metroline-ops-state"))
GIT = os.environ.get("METROLINE_GIT") or shutil.which("git", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin") or "git"
INFO = os.path.join(STATE, "launcher.json")
GOOD = os.path.join(STATE, "last_good_sha")


def git(*args, timeout=180):
    p = subprocess.run([GIT, "-C", REPO, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip()


def sha():
    rc, out = git("rev-parse", "--short", "HEAD", timeout=20)
    return out if rc == 0 else "unknown"


def compiles():
    files = [os.path.join(REPO, "agent", "ops_agent.py"), os.path.join(REPO, "agent", "health.py"),
             os.path.join(REPO, "fleet", "poi_fleet.py"), os.path.join(REPO, "fleet", "poi_bake.py")]
    p = subprocess.run([sys.executable, "-m", "py_compile", *[f for f in files if os.path.exists(f)]],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return p.returncode == 0, p.stdout.strip()[-800:]


def main():
    os.makedirs(STATE, exist_ok=True)
    info = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git": GIT, "python": sys.executable}
    before = sha()
    try:
        rc, out = git("fetch", "--quiet", "origin")
        if rc != 0:
            info["fetch"] = f"failed: {out[-300:]}"
        else:
            rc, out = git("merge", "--ff-only", "--quiet", "origin/main")
            if rc != 0:
                rc2, dirty = git("status", "--porcelain")
                if rc2 == 0 and not dirty:
                    rc3, out3 = git("reset", "--hard", "origin/main")
                    info["update"] = "reset to origin/main (history rewritten upstream)" if rc3 == 0 else f"reset failed: {out3[-300:]}"
                else:
                    info["update"] = f"cannot fast-forward and the checkout has local changes: {dirty[:300] or out[-300:]}"
            else:
                info["update"] = "fast-forwarded" if sha() != before else "up to date"
    except Exception as e:  # noqa: BLE001
        info["fetch"] = f"error: {e!r}"
    ok, msg = compiles()
    if not ok:
        info["compile"] = f"FAILED at {sha()}: {msg}"
        good = open(GOOD).read().strip() if os.path.exists(GOOD) else ""
        if good:
            rc, out = git("checkout", "--quiet", good)
            info["rollback"] = f"checked out last good {good}" if rc == 0 else f"rollback failed: {out[-200:]}"
            ok, msg = compiles()
    if ok:
        info["compile"] = "ok"
        with open(GOOD, "w") as f:
            f.write(sha())
    info["sha"] = sha()
    with open(INFO + ".tmp", "w") as f:
        json.dump(info, f, indent=2)
    os.replace(INFO + ".tmp", INFO)
    if not ok:
        print(json.dumps(info), flush=True)
        return 1
    env = dict(os.environ, METROLINE_OPS_REPO=REPO, METROLINE_OPS_STATE=STATE, METROLINE_GIT=GIT)
    return subprocess.call([sys.executable, os.path.join(REPO, "agent", "ops_agent.py")], cwd=REPO, env=env)


if __name__ == "__main__":
    sys.exit(main())
