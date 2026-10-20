#!/usr/bin/env bash
# Deploy to one SSH host. --dry-run prints every transport command without contacting it.
# Usage: deploy.sh HOST [--dry-run]   (HOST is an ssh destination, e.g. a Host alias in ~/.ssh/config)
# A real run prints each step's elapsed time and the service status before and after, and checks after
# the restart that the remote tree equals this checkout. Lines starting with `backup:`, `before:`,
# `after:`, `check:` and `deploy:` are stable for callers that keep a log.
set -euo pipefail
USAGE='Usage: deploy.sh HOST [--dry-run]'
NODE=
DRY_RUN=0
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "$USAGE" >&2; exit 2 ;;
    *) if [[ -n "$NODE" ]]; then echo "$USAGE" >&2; exit 2; fi
       NODE="$1" ;;
  esac
  shift
done
[[ -n "$NODE" ]] || { echo "$USAGE" >&2; exit 2; }
REMOTE="${DEPLOY_REMOTE:-$NODE}"
DEPLOY_ACCOUNT="${DEPLOY_USER:-ubuntu}"
APP_DIR="${DEPLOY_APP_DIR:-/home/$DEPLOY_ACCOUNT/chanapp}"
INSTANCE_CONFIG="${DEPLOY_INSTANCE_CONFIG:-/home/$DEPLOY_ACCOUNT/.config/chanapp/instance.json}"
PYTHON="${DEPLOY_PYTHON:-python3.12}"
ZONE="${DEPLOY_TZ:-Asia/Shanghai}"
DEFAULT_PORT=8899
PORT="${DEPLOY_PORT:-$DEFAULT_PORT}"
BACKUP_KEEP="${DEPLOY_BACKUP_KEEP:-5}"
[[ "$REMOTE" =~ ^[a-zA-Z0-9_@.-]+$ && "$DEPLOY_ACCOUNT" =~ ^[a-z_][a-z0-9_-]*$ \
   && "$APP_DIR" =~ ^/[a-zA-Z0-9_/-]+/chanapp$ && "$PYTHON" =~ ^[a-zA-Z0-9_./-]+$ \
   && "$INSTANCE_CONFIG" =~ ^/[a-zA-Z0-9_./-]+$ \
   && "$ZONE" =~ ^[a-zA-Z0-9_+/-]+$ && "$PORT" =~ ^[0-9]+$ ]] \
   || { echo 'Invalid deployment configuration' >&2; exit 2; }
((10#$PORT > 0 && 10#$PORT < 65536)) || { echo 'Invalid deployment port' >&2; exit 2; }
[[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] && ((10#$BACKUP_KEEP >= 1)) \
  || { echo 'Invalid DEPLOY_BACKUP_KEEP (integer >= 1)' >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The package directory is synced as the app dir; requirements.txt sits beside it in the repo root.
REQUIREMENTS="$(dirname "$ROOT")/requirements.txt"
[[ -f "$REQUIREMENTS" ]] || { echo 'requirements.txt not found' >&2; exit 2; }
run() {
  if ((DRY_RUN)); then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# Elapsed seconds since start and since the previous step (bash 3.2 on macOS has no sub-second clock).
LAST_STEP=$SECONDS
step() {
  ((DRY_RUN)) && return 0
  printf '[%4ss +%ss] %s\n' "$SECONDS" "$((SECONDS - LAST_STEP))" "$1"
  LAST_STEP=$SECONDS
}
# Both the sync and the post-deploy comparison use this list: what it excludes is never deployed or checked.
SYNC_EXCLUDES=(--exclude .git --exclude .runtime --exclude .cache --exclude __pycache__
  --exclude '.env*' --exclude .venv --exclude .DS_Store --exclude watchlist.json
  --exclude tmp --exclude '.public' --exclude '.openai' --exclude '.secrets'
  --exclude '*credentials*.json' --exclude '*token*.json' --exclude /requirements.txt)
# One status line from the running service, before and after: what a deployment must not change.
STATUS_FN=$(cat <<'STATUS'
status_line() {
  local body
  body=$(curl -fsS --max-time 10 "http://127.0.0.1:$1/api/status" 2>/dev/null) || { echo 'status: unavailable'; return 0; }
  printf '%s' "$body" | "$2" -c '
import hashlib, json, sys
d = json.load(sys.stdin)
ds = d.get("datasets") or []
total = lambda key: sum(x.get(key) or 0 for x in ds)
bindings = sorted("%s/%s/%s=%s#%s" % (p.get("market"), p.get("kind"), p.get("item"), p.get("active"), p.get("binding_gen"))
                  for p in d.get("probes") or [])
print("status: mode=%s collector=%s bindings=%s upstream=%d datasets=%d stale=%d open_gaps=%d known_gaps=%d pending_review=%d" % (
    d.get("mode"), "on" if d.get("enabled") else "off", hashlib.sha256("\n".join(bindings).encode()).hexdigest()[:8],
    len(d.get("upstream") or []), len(ds), sum(1 for x in ds if x.get("stale")),
    total("open_gaps"), total("known_gaps"), total("pending_review")))' 2>/dev/null || echo 'status: unavailable'
}
STATUS
)
printf 'node=%s remote=%s app=%s port=%s dry_run=%s\n' "$NODE" "$REMOTE" "$APP_DIR" "$PORT" "$DRY_RUN"
# Refuse an accidental demo deployment before modifying the remote application.
# This file is provisioned by the owner; deployment only reads it.
PREFLIGHT_SCRIPT=$(cat <<'PREFLIGHT'
set -euo pipefail
app=$1
config=$2
python_command=$3
"$python_command" - "$app" "$config" <<'PYCONFIG'
import json
from pathlib import Path
import sys
app, config = (Path(value).resolve() for value in sys.argv[1:])
if config == app or app in config.parents:
    sys.exit('Instance config must be outside the application directory')
try:
    data = json.loads(config.read_text())
except (OSError, ValueError):
    sys.exit('Place a readable instance config before deployment')
if not isinstance(data, dict) or data.get('mode') != 'real':
    sys.exit('Production instance config must explicitly set mode=real')
PYCONFIG
PREFLIGHT
)
PREFLIGHT_SCRIPT="$PREFLIGHT_SCRIPT"$'\n'"$STATUS_FN"$'\n''status_line "$4" "$3"'
if ((DRY_RUN)); then
  printf '%q ' ssh "$REMOTE" bash -s -- "$APP_DIR" "$INSTANCE_CONFIG" "$PYTHON" "$PORT"
  printf " <<'PREFLIGHT'\n%s\nPREFLIGHT\n" "$PREFLIGHT_SCRIPT"
else
  BEFORE=$(ssh "$REMOTE" bash -s -- "$APP_DIR" "$INSTANCE_CONFIG" "$PYTHON" "$PORT" <<< "$PREFLIGHT_SCRIPT")
  BEFORE=$(grep '^status:' <<< "$BEFORE" || echo 'status: unavailable')
  printf 'before: %s\n' "${BEFORE#status: }"
fi
step 'preflight and status'
# Remote restore point before rsync --delete: what the sync replaces (the app dir minus .venv and the
# .cache, .runtime and tmp state it never touches), unit, pip freeze and watchlist. A live SQLite in
# .cache would not be a consistent copy anyway; take facts.snapshot for that (docs/operations.md).
# Any failure aborts the deployment (set -e).
BACKUP_SCRIPT=$(cat <<'BACKUP'
set -euo pipefail
app=$1
keep=$2
if [[ ! -d "$app" ]]; then echo 'backup: no existing app dir, first deployment'; exit 0; fi
dir="$HOME/chanapp-deploy-backups"
mkdir -p "$dir"
chmod 700 "$dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -czf "$work/app.tgz" --exclude=./.venv --exclude=./.cache --exclude=./.runtime --exclude=./tmp -C "$app" .
if [[ -f /etc/systemd/system/chanapp.service ]]; then cp /etc/systemd/system/chanapp.service "$work/"; fi
if [[ -x "$app/.venv/bin/python" ]]; then "$app/.venv/bin/python" -m pip freeze > "$work/pip-freeze.txt"; fi
watchlist="$(dirname "$app")/chanapp.watchlist.json"
if [[ -f "$watchlist" ]]; then cp "$watchlist" "$work/"; fi
tar -cf "$dir/deploy-$stamp.tar.partial" -C "$work" .
mv "$dir/deploy-$stamp.tar.partial" "$dir/deploy-$stamp.tar"
ls -1t "$dir"/deploy-*.tar | tail -n +$((keep + 1)) | xargs -r rm -f
printf 'backup: %s\n' "$dir/deploy-$stamp.tar"
BACKUP
)
if ((DRY_RUN)); then
  printf '%q ' ssh "$REMOTE" bash -s -- "$APP_DIR" "$BACKUP_KEEP"
  printf " <<'BACKUP'\n%s\nBACKUP\n" "$BACKUP_SCRIPT"
else
  ssh "$REMOTE" bash -s -- "$APP_DIR" "$BACKUP_KEEP" <<< "$BACKUP_SCRIPT"
fi
step 'backup'
run rsync -az --delete "${SYNC_EXCLUDES[@]}" "$ROOT/" "$REMOTE:$APP_DIR/"
run rsync -az "$REQUIREMENTS" "$REMOTE:$APP_DIR/requirements.txt"
step 'sync'
REMOTE_SCRIPT=$(cat <<'REMOTE'
set -euo pipefail
app=$1
account=$2
port=$3
python_command=$4
zone=$5
instance_config=$6
cd "$app"
uv_command=$(command -v uv || true)
if [[ -z "$uv_command" && -x "$HOME/.local/bin/uv" ]]; then uv_command="$HOME/.local/bin/uv"; fi
if [[ ! -x .venv/bin/python ]]; then
  if [[ -e .venv ]]; then
    echo 'Existing .venv is incomplete; repair it explicitly. Deployment will not delete it.' >&2
    exit 1
  fi
  if [[ -n "$uv_command" ]]; then
    "$uv_command" venv --python "$python_command" .venv
  else
    "$python_command" -c 'import venv, ensurepip' || {
      echo 'Python venv/ensurepip unavailable and uv not installed; provision one before deployment.' >&2
      exit 1
    }
    "$python_command" -m venv .venv
  fi
fi
if [[ -n "$uv_command" ]]; then
  "$uv_command" pip install --python .venv/bin/python -q -r requirements.txt
else
  .venv/bin/python -m pip --version >/dev/null || {
    echo 'Existing venv has no pip and uv is unavailable; provision an installer.' >&2
    exit 1
  }
  .venv/bin/python -m pip install -q -r requirements.txt
fi
# Full validation with the new code and the unit's credential files: a rejected config would
# stop the service on restart, so abort here and leave the running process untouched.
.venv/bin/python scripts/check_instance_config.py "$instance_config" "/home/$account/.config/chanapp/secrets.env" "$app/.env"
parent=$(dirname "$app")
watchlist="$parent/chanapp.watchlist.json"
if [[ ! -e "$watchlist" && -f "$app/watchlist.json" ]]; then
  cp -n "$app/watchlist.json" "$watchlist"
fi
unit=$(mktemp)
trap 'rm -f "$unit"' EXIT
sed -e "s|@USER@|$account|g" -e "s|@APP_DIR@|$app|g" \
    -e "s|@PARENT_DIR@|$parent|g" -e "s|@PORT@|$port|g" \
    -e "s|@INSTANCE_CONFIG@|$instance_config|g" -e "s|@TIMEZONE@|$zone|g" "$app/scripts/chanapp.service" > "$unit"
sudo install -m 644 "$unit" /etc/systemd/system/chanapp.service
sudo systemctl daemon-reload
sudo systemctl enable chanapp >/dev/null
sudo systemctl restart chanapp
# Keep an already-installed checker in sync without enabling it on other nodes.
if systemctl is-enabled --quiet chanapp-selfcheck.timer; then
  bash scripts/install_selfcheck.sh "$app" "$account" "$port" "$zone"
fi
code=000
for attempt in {1..10}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || true)
  if [[ "$code" == 200 ]]; then printf 'remote health: HTTP 200 (port %s)\n' "$port"; exit 0; fi
  sleep 2
done
printf 'Remote health failed (HTTP %s); inspect journalctl -u chanapp\n' "$code" >&2
exit 1
REMOTE
)
# Inputs above use a restricted alphabet so SSH remote-shell joining is safe.
if ((DRY_RUN)); then
  printf '%q ' ssh "$REMOTE" bash -s -- "$APP_DIR" "$DEPLOY_ACCOUNT" "$PORT" "$PYTHON" "$ZONE" "$INSTANCE_CONFIG"
  printf " <<'REMOTE'\n%s\nREMOTE\n" "$REMOTE_SCRIPT"
else
  ssh "$REMOTE" bash -s -- "$APP_DIR" "$DEPLOY_ACCOUNT" "$PORT" "$PYTHON" "$ZONE" "$INSTANCE_CONFIG" <<< "$REMOTE_SCRIPT"
fi
step 'install and restart'
# After the restart: status again and the new process's own log.
POSTCHECK_SCRIPT=$(cat <<'POSTCHECK'
set -euo pipefail
port=$1
python_command=$2
status_line "$port" "$python_command"
pid=$(systemctl show chanapp -p MainPID --value)
if logs=$(journalctl -u chanapp _PID="$pid" --no-pager 2>/dev/null); then
  errors=$(grep -cE 'ERROR|Traceback' <<< "$logs" || true)
else
  errors=unknown
fi
printf 'service: pid=%s log_errors=%s\n' "$pid" "$errors"
POSTCHECK
)
POSTCHECK_SCRIPT="$STATUS_FN"$'\n'"$POSTCHECK_SCRIPT"
if ((DRY_RUN)); then
  printf '%q ' ssh "$REMOTE" bash -s -- "$PORT" "$PYTHON"
  printf " <<'POSTCHECK'\n%s\nPOSTCHECK\n" "$POSTCHECK_SCRIPT"
  run rsync -anc --delete --itemize-changes "${SYNC_EXCLUDES[@]}" "$ROOT/" "$REMOTE:$APP_DIR/"
  exit 0
fi
POST=$(ssh "$REMOTE" bash -s -- "$PORT" "$PYTHON" <<< "$POSTCHECK_SCRIPT")
AFTER=$(grep '^status:' <<< "$POST" || echo 'status: unavailable')
printf 'after: %s\n' "${AFTER#status: }"
grep -v '^status:' <<< "$POST" || true
# The remote tree must now equal this checkout for everything the sync covers.
# Lines starting with '.' differ only in times or modes; a failed comparison is not a clean tree.
tree=same
if ITEMS=$(rsync -anc --delete --itemize-changes "${SYNC_EXCLUDES[@]}" "$ROOT/" "$REMOTE:$APP_DIR/"); then
  DRIFT=$(grep -v '^\.' <<< "$ITEMS" || true)
  [[ -z "$DRIFT" ]] || { tree=differs; printf 'tree differs:\n%s\n' "$DRIFT" >&2; }
else
  tree=unchecked
fi
step 'post-deploy checks'
# Status is for a person to read (an intended change, e.g. a new binding, also moves it); only a tree
# that differs from this commit, or could not be compared, fails the deployment.
if [[ "$BEFORE" == 'status: unavailable' ]]; then
  status=no-baseline
elif [[ "$AFTER" == "$BEFORE" ]]; then
  status=unchanged
else
  status=changed
fi
errors=$(sed -n 's/.* log_errors=//p' <<< "$POST")
printf 'check: status=%s log_errors=%s tree=%s\n' "$status" "${errors:-unknown}" "$tree"
if [[ "$tree" != same ]]; then
  printf 'deploy: FAILED remote tree %s after %ss; the new code is running, redeploy the last good commit\n' \
    "$tree" "$SECONDS" >&2
  exit 1
fi
printf 'deploy: ok in %ss\n' "$SECONDS"
