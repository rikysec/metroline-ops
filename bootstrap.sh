#!/bin/bash
# metroline-ops bootstrap — run ONCE on the data host (the home Mac), as the normal user, no sudo:
#
#   curl -fsSL https://raw.githubusercontent.com/rikysec/metroline-ops/main/bootstrap.sh | bash
#
# Installs the ops agent (~/metroline-ops, a per-user LaunchAgent "app.metroline.ops" that ticks every 10 minutes),
# runs the first tick and prints the published status. Idempotent: re-running updates the checkout and the plist.
# It never touches Caddy, cloudflared, LaunchDaemons, ~/.ssh or anything outside ~/metroline-ops*, ~/Library/LaunchAgents
# and ~/metroline-data/demand/_ops.
set -u
REPO_URL="https://github.com/rikysec/metroline-ops.git"
LABEL="app.metroline.ops"
OPS="$HOME/metroline-ops"
STATE="$HOME/metroline-ops-state"
DATA="$HOME/metroline-data/demand"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mBOOTSTRAP FAILED: %s\033[0m\n' "$*"; exit 1; }

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
[ -d "$DATA" ] || echo "note: $DATA does not exist yet (the data host is not set up?) — creating it so the agent can publish its status"
mkdir -p "$DATA/_ops" "$STATE" "$HOME/Library/LaunchAgents" || fail "cannot create folders under \$HOME"

say "2/6 checkout $OPS"
if [ -d "$OPS/.git" ]; then
  "$GIT" -C "$OPS" fetch --quiet origin && "$GIT" -C "$OPS" merge --ff-only --quiet origin/main || echo "warning: could not fast-forward (local changes?) — keeping the current checkout"
else
  [ -e "$OPS" ] && fail "$OPS exists but is not a git checkout — move it away and re-run"
  "$GIT" clone --quiet "$REPO_URL" "$OPS" || fail "git clone failed (network?)"
fi
echo "at $("$GIT" -C "$OPS" rev-parse --short HEAD)"

say "3/6 LaunchAgent $LABEL (every 10 minutes, as $USER, no sudo)"
sed -e "s|__PYTHON__|$PY|g" -e "s|__HOME__|$HOME|g" "$OPS/agent/app.metroline.ops.plist" > "$PLIST.tmp" && mv "$PLIST.tmp" "$PLIST" || fail "cannot write $PLIST"
plutil -lint "$PLIST" >/dev/null || fail "plist does not validate"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" || fail "launchctl bootstrap failed (is a user session active? run this from a Terminal of the logged-in user)"
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

say "4/6 first tick now (pull, jobs, status) — may take a minute"
METROLINE_OPS_REPO="$OPS" METROLINE_OPS_STATE="$STATE" METROLINE_DATA="$DATA" "$PY" "$OPS/agent/ops_agent.py" || echo "warning: the first tick returned an error — see $STATE/agent.log"

say "5/6 verify"
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|last exit|program|interval" | head -5
echo "--- $DATA/_ops/status.json"
"$PY" - "$DATA/_ops/status.json" <<'PYEOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    print(json.dumps({k: s.get(k) for k in ("tick", "repo_sha", "host", "jobs", "fleet")}, indent=2, ensure_ascii=False)[:3000])
except Exception as e:
    print("status not readable:", e)
PYEOF

say "6/6 done"
cat <<EOF
The agent now runs every 10 minutes on its own. From anywhere:
  curl -s "https://data.metroline.app/demand/_ops/status.json?t=\$(date +%s)" | python3 -m json.tool
Logs on this Mac: $STATE/agent.log, $STATE/job_poi_fleet.log. Stop/uninstall: launchctl bootout gui/\$(id -u)/$LABEL
EOF
