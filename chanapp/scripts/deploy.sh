#!/usr/bin/env bash
# Deploy to one SSH host. --dry-run prints every transport command without contacting it.
# Usage: deploy.sh HOST [--dry-run]   (HOST is an ssh destination, e.g. a Host alias in ~/.ssh/config)
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
if ((DRY_RUN)); then
  printf '%q ' ssh "$REMOTE" bash -s -- "$APP_DIR" "$INSTANCE_CONFIG" "$PYTHON"
  printf " <<'PREFLIGHT'\n%s\nPREFLIGHT\n" "$PREFLIGHT_SCRIPT"
else
  ssh "$REMOTE" bash -s -- "$APP_DIR" "$INSTANCE_CONFIG" "$PYTHON" <<< "$PREFLIGHT_SCRIPT"
fi
# Remote restore point before rsync --delete: app dir (incl. .cache, minus .venv), unit,
# pip freeze and watchlist. Any failure aborts the deployment (set -e).
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
tar -czf "$work/app.tgz" --exclude=./.venv -C "$app" .
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
run rsync -az --delete --exclude .git --exclude .runtime --exclude .cache --exclude __pycache__ \
  --exclude '.env*' --exclude .venv --exclude .DS_Store --exclude watchlist.json \
  --exclude tmp --exclude '.public' --exclude '.openai' --exclude '.secrets' \
  --exclude '*credentials*.json' --exclude '*token*.json' --exclude /requirements.txt \
  "$ROOT/" "$REMOTE:$APP_DIR/"
run rsync -az "$REQUIREMENTS" "$REMOTE:$APP_DIR/requirements.txt"
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
