#!/usr/bin/env bash
# Run on the application host. Does not restart chanapp or install dependencies.
set -euo pipefail
app=${1:?Usage: install_selfcheck.sh APP_DIR USER PORT TIMEZONE}
account=${2:?Missing service user}
port=${3:?Missing local service port}
zone=${4:?Missing timezone}
[[ "$app" =~ ^/[a-zA-Z0-9_/-]+/chanapp$ && "$account" =~ ^[a-z_][a-z0-9_-]*$ \
   && "$port" =~ ^[0-9]+$ && "$zone" =~ ^[a-zA-Z0-9_+/-]+$ ]] || exit 2
((10#$port > 0 && 10#$port < 65536)) || exit 2
parent=$(dirname "$app")
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
sed -e "s|@USER@|$account|g" -e "s|@APP_DIR@|$app|g" \
    -e "s|@PARENT_DIR@|$parent|g" -e "s|@PORT@|$port|g" \
    -e "s|@TIMEZONE@|$zone|g" "$app/scripts/chanapp-selfcheck.service" > "$staging/chanapp-selfcheck.service"
cp "$app/scripts/chanapp-selfcheck.timer" "$staging/chanapp-selfcheck.timer"
systemd-analyze verify "$staging/chanapp-selfcheck.service" "$staging/chanapp-selfcheck.timer"
sudo install -m 644 "$staging/chanapp-selfcheck.service" "$staging/chanapp-selfcheck.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chanapp-selfcheck.timer
sudo systemctl start chanapp-selfcheck.service
