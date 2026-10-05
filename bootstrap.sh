#!/bin/bash
# metroline-ops bootstrap — run ONCE on the data host (the home Mac), as the normal user, no sudo:
#
#   curl -fsSL https://raw.githubusercontent.com/rikysec/metroline-ops/main/bootstrap.sh | bash
#
# Installs the ops agent (~/metroline-ops, a per-user LaunchAgent "app.metroline.ops" that ticks every 10 minutes
# through a launcher kept OUTSIDE the repo), runs the first tick and prints the published status. Idempotent:
# re-running updates the checkout, the launcher and the plist. It never touches Caddy, cloudflared, other
# LaunchAgents/Daemons, ~/.ssh or anything outside ~/metroline-ops*, its own plist and ~/metroline-data/demand/_ops.
#
# Variant for a Mac that reboots without anyone logging in (a per-user LaunchAgent only runs inside a login session):
#   curl -fsSL https://raw.githubusercontent.com/rikysec/metroline-ops/main/bootstrap.sh | sudo -E bash -s -- --daemon
# installs the SAME agent as a LaunchDaemon running as the invoking user (like app.metroline.caddy) — the one step
# that needs the owner's password; it replaces the per-user agent if present.
set -u
REPO_URL="https://github.com/rikysec/metroline-ops.git"
LABEL="app.metroline.ops"
MODE="agent"; [ "${1:-}" = "--daemon" ] && MODE="daemon"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mBOOTSTRAP FAILED: %s\033[0m\n' "$*"; exit 1; }

if [ "$MODE" = "daemon" ]; then
  [ "$(id -u)" = 0 ] || fail "--daemon must run with sudo:  curl … | sudo -E bash -s -- --daemon"
  RUN_USER="${SUDO_USER:-}"; { [ -n "$RUN_USER" ] && [ "$RUN_USER" != root ]; } || fail "cannot tell the real user (SUDO_USER empty) — run it from the owner's own Terminal with sudo"
  HOME="$(dscl . -read "/Users/$RUN_USER" NFSHomeDirectory | awk '{print $2}')"
  [ -d "$HOME" ] || fail "home folder of $RUN_USER not found"
  RUN_UID="$(id -u "$RUN_USER")"
else
  [ "$(id -u)" != 0 ] || fail "do not run this with sudo (root-owned files would land in the user's home); the LaunchDaemon variant is:  … | sudo -E bash -s -- --daemon"
  RUN_USER="$(id -un)"; RUN_UID="$(id -u)"
fi
OPS="$HOME/metroline-ops"
STATE="$HOME/metroline-ops-state"
DATA="$HOME/metroline-data/demand"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DAEMON_PLIST="/Library/LaunchDaemons/$LABEL.plist"
asuser() { if [ "$MODE" = "daemon" ]; then sudo -u "$RUN_USER" -H "$@"; else "$@"; fi; }

say "1/6 tool check (no sudo, no Command Line Tools installation is triggered)"
CLT_OK=0; xcode-select -p >/dev/null 2>&1 && CLT_OK=1
PY=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [ -x "$cand" ] && PY="$cand" && break
done
if [ -z "$PY" ]; then
  if [ "$CLT_OK" = 1 ] && [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else
    fail "no python3 found that is safe to use (Homebrew python3 missing and Command Line Tools absent). Install python3 with Homebrew or python.org, then re-run."; fi
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || fail "python3 at $PY is older than 3.9"
GIT=""
if [ "$CLT_OK" = 1 ] && [ -x /usr/bin/git ]; then GIT=/usr/bin/git; fi
for cand in /opt/homebrew/bin/git /usr/local/bin/git; do [ -z "$GIT" ] && [ -x "$cand" ] && GIT="$cand"; done
[ -n "$GIT" ] || fail "git not found (Command Line Tools absent and no Homebrew git)"
echo "python3: $PY ($("$PY" --version 2>&1))"
echo "git:     $GIT ($("$GIT" --version 2>&1))"
if [ "$MODE" = "agent" ] && launchctl print "system/$LABEL" >/dev/null 2>&1; then
  fail "$LABEL is installed as a LaunchDaemon on this Mac; re-run the --daemon variant to update it (or 'sudo launchctl bootout system/$LABEL' first)"
fi
[ -d "$DATA" ] || echo "note: $DATA does not exist yet (the data host is not set up?) — creating it so the agent can publish its status"
asuser mkdir -p "$DATA/_ops" "$STATE" "$HOME/Library/LaunchAgents" || fail "cannot create folders under $HOME"
[ "$(stat -f %u "$STATE")" = "$RUN_UID" ] || fail "$STATE is not owned by $RUN_USER (was a previous run done with sudo? fix the ownership and re-run)"

say "2/6 checkout $OPS"
if [ -d "$OPS/.git" ]; then
  asuser "$GIT" -C "$OPS" fetch --quiet origin && asuser "$GIT" -C "$OPS" merge --ff-only --quiet origin/main || echo "warning: could not fast-forward (local changes?) — keeping the current checkout; the launcher resets a clean mirror on its own"
else
  [ -e "$OPS" ] && fail "$OPS exists but is not a git checkout — move it away and re-run"
  asuser "$GIT" clone --quiet "$REPO_URL" "$OPS" || fail "git clone failed (network?)"
fi
echo "at $("$GIT" -C "$OPS" rev-parse --short HEAD)"
# The launcher lives outside the repo (a bad push can never brick the control channel); refreshed only here.
asuser cp "$OPS/agent/launcher.py" "$STATE/launcher.py" || fail "cannot install the launcher"

say "3/6 $([ "$MODE" = daemon ] && echo LaunchDaemon || echo LaunchAgent) $LABEL (every 10 minutes, as $RUN_USER)"
if [ "$MODE" = "daemon" ]; then PLIST="$DAEMON_PLIST"; else PLIST="$AGENT_PLIST"; fi
# Rendered by Python (no sed: paths with '&' or '|' would break it); UserName only for the daemon (ignored for agents).
"$PY" - "$OPS/agent/app.metroline.ops.plist" "$PLIST.tmp" "$PY" "$HOME" "$STATE" "$GIT" "$MODE" "$RUN_USER" <<'PYEOF'
import sys
from xml.sax.saxutils import escape
src, dst, py, home, state, git, mode, user = sys.argv[1:9]
t = open(src).read()
for k, v in (("__PYTHON__", py), ("__HOME__", home), ("__STATE__", state), ("__GIT__", git), ("__LAUNCH__", mode)):
    t = t.replace(k, escape(v))
if mode == "daemon":
    t = t.replace("<key>Label</key>", f"<key>UserName</key><string>{escape(user)}</string>\n    <key>Label</key>", 1)
open(dst, "w").write(t)
PYEOF
[ -s "$PLIST.tmp" ] || fail "cannot render the plist"
mv "$PLIST.tmp" "$PLIST" || fail "cannot write $PLIST"
plutil -lint "$PLIST" >/dev/null || fail "plist does not validate"
if [ "$MODE" = "daemon" ]; then
  chown root:wheel "$PLIST" && chmod 644 "$PLIST"
  launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap system "$PLIST" || fail "launchctl bootstrap (system) failed"
  launchctl enable "system/$LABEL" 2>/dev/null || true
  # only now retire the per-user agent (never a moment without a scheduler)
  launchctl bootout "gui/$RUN_UID/$LABEL" >/dev/null 2>&1 || true
  rm -f "$AGENT_PLIST"
  DOMAIN="system"
else
  chmod 644 "$PLIST"
  launchctl bootout "gui/$RUN_UID/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$RUN_UID" "$PLIST" || fail "launchctl bootstrap failed (is a user session active? run this from a Terminal of the logged-in user)"
  launchctl enable "gui/$RUN_UID/$LABEL" 2>/dev/null || true
  DOMAIN="gui/$RUN_UID"
fi

say "4/6 first tick now (update, compile check, jobs, status) — may take a minute"
asuser env METROLINE_OPS_REPO="$OPS" METROLINE_OPS_STATE="$STATE" METROLINE_DATA="$DATA" METROLINE_GIT="$GIT" METROLINE_LAUNCH="$MODE" HOME="$HOME" \
  "$PY" "$STATE/launcher.py" || echo "warning: the first tick returned an error — see $STATE/agent.log and $STATE/launcher.json"

say "5/6 verify"
launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|last exit|program|interval" | head -5
echo "--- $DATA/_ops/status.json"
"$PY" - "$DATA/_ops/status.json" <<'PYEOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    print(json.dumps({k: s.get(k) for k in ("tick", "repo_sha", "launcher", "host", "jobs", "fleet", "errors")}, indent=2, ensure_ascii=False)[:3500])
except Exception as e:
    print("status not readable:", e)
PYEOF

say "6/6 done"
cat <<EOF
The agent now runs every 10 minutes on its own ($DOMAIN/$LABEL). From anywhere:
  curl -s "https://data.metroline.app/demand/_ops/status.json?t=\$(date +%s)" | python3 -m json.tool
Logs on this Mac: $STATE/agent.log, $STATE/job_poi_fleet.log, $STATE/launcher.json. Stop/uninstall: launchctl bootout $DOMAIN/$LABEL
EOF
