#!/bin/bash
# Installs the GCP launcher on an Ubuntu 24.04 VM and starts it behind Caddy, which gets the TLS certificate. Run it as root
# from the copy of launchers/gcp on the VM; it is safe to run again to update the launcher.
#
#   sudo bash deploy/install.sh NAME
#
# NAME is the DNS name the launcher answers on, for example launcher.example.com. Its A record must point at the VM's address
# before this runs, because Caddy asks for the certificate for NAME when it starts. NAME can also be given as LAUNCHER_NAME in
# the environment (an argument wins); there is no memory of it between runs, so give the same NAME each time.
#
# What it does: installs python3, python3-venv, rsync and Caddy (from Caddy's own apt repository); makes the user
# superci-launcher; copies this directory to /opt/superci-launcher without .venv, tests and caches; makes or refreshes the
# venv there from requirements.txt; installs the systemd unit, the sweep service and timer, and /etc/caddy/Caddyfile; enables
# and restarts the launcher and Caddy; checks that http://127.0.0.1:8080/health answers {"agent":"gcp"}; and then starts the
# timer that calls POST /sweep every ten minutes.
#
# The launcher's configuration is /etc/superci-launcher.env. The first run writes it from env.example (root-owned, mode 0600)
# and stops, because the launcher cannot start until its placeholders are filled in; fill them in and run again. An existing
# file is never overwritten.
set -euo pipefail
umask 022
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

trap 'echo "superci: install failed at line $LINENO" >&2' ERR

app=/opt/superci-launcher
env_file=/etc/superci-launcher.env
service_user=superci-launcher
unit=superci-launcher.service
sweep_service=superci-launcher-sweep.service
sweep_timer=superci-launcher-sweep.timer
caddy_keyring=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
caddy_list=/etc/apt/sources.list.d/caddy-stable.list
health_url=http://127.0.0.1:8080/health
health_answer='{"agent":"gcp"}'

die() {
  echo "superci: $*" >&2
  exit 1
}

usage() {
  echo "usage: sudo bash $0 NAME   (NAME: the launcher's DNS name; LAUNCHER_NAME in the environment also gives it)" >&2
  exit 2
}

aptget() {
  apt-get -o DPkg::Lock::Timeout=600 -o Acquire::Retries=5 -y -q "$@"
}

[ $# -le 1 ] || usage
name=${1:-${LAUNCHER_NAME:-}}
[ -n "$name" ] || usage
[[ $name =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || die "'$name' is not a DNS name such as launcher.example.com"
[ "$(id -u)" -eq 0 ] || die "run this as root: sudo bash $0 $name"

deploy=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
src=$(dirname "$deploy")
for f in app.py launcher.py requirements.txt deploy/superci-launcher.service deploy/superci-launcher-sweep.service \
  deploy/superci-launcher-sweep.timer deploy/Caddyfile deploy/env.example; do
  [ -f "$src/$f" ] || die "$src/$f not found: run this from a copy of launchers/gcp"
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "superci: installing packages"
aptget update
aptget install ca-certificates curl gpg python3 python3-venv rsync debian-keyring debian-archive-keyring apt-transport-https
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "python3 is older than 3.12 (Ubuntu 24.04 has 3.12)"

# Caddy comes from Caddy's own repository, as https://caddyserver.com/docs/install describes for Debian and Ubuntu.
if [ ! -f "$caddy_keyring" ] || [ ! -f "$caddy_list" ]; then
  echo "superci: adding Caddy's apt repository"
  curl -fsSL --retry 5 https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$work/caddy.key"
  curl -fsSL --retry 5 https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o "$work/caddy-stable.list"
  gpg --dearmor --yes -o "$work/caddy.gpg" "$work/caddy.key"
  install -m 0644 "$work/caddy.gpg" "$caddy_keyring"
  install -m 0644 "$work/caddy-stable.list" "$caddy_list"
  aptget update
fi
aptget install caddy

id -u "$service_user" > /dev/null 2>&1 \
  || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$service_user"

echo "superci: copying the launcher to $app"
install -d -m 0755 "$app"
if [ "$src" != "$app" ]; then
  # --delete leaves the excluded paths in $app alone, so the venv there survives.
  rsync -a --delete --no-owner --no-group --chmod=u=rwX,go=rX \
    --exclude=/.venv --exclude=/tests --exclude=__pycache__ --exclude=.pytest_cache \
    "$src"/ "$app"/
fi

echo "superci: installing the Python dependencies"
[ -x "$app/.venv/bin/python" ] || python3 -m venv "$app/.venv"
"$app/.venv/bin/pip" install --disable-pip-version-check --no-cache-dir -q -r "$app/requirements.txt"

echo "superci: installing the systemd units and the Caddyfile for $name"
for f in "$unit" "$sweep_service" "$sweep_timer"; do
  install -m 0644 "$deploy/$f" "/etc/systemd/system/$f"
done
sed "s/__NAME__/$name/g" "$deploy/Caddyfile" > "$work/Caddyfile"
# (validated with its data and config directories under $work, so that this run leaves nothing in root's home)
XDG_DATA_HOME=$work/data XDG_CONFIG_HOME=$work/config caddy validate --config "$work/Caddyfile" --adapter caddyfile
install -m 0644 "$work/Caddyfile" /etc/caddy/Caddyfile

if [ ! -e "$env_file" ]; then
  install -m 0600 -o root -g root "$deploy/env.example" "$env_file"
  echo "superci: wrote $env_file from env.example"
fi
chown root:root "$env_file"
chmod 0600 "$env_file"
unfilled=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=.*<[^<>]*>' "$env_file" | cut -d= -f1 | tr '\n' ' ' || true)
if [ -n "$unfilled" ]; then
  echo "superci: $env_file still has placeholders for: $unfilled" >&2
  die "fill them in (sudoedit $env_file), then run this again; the launcher is not started"
fi

echo "superci: starting the launcher and Caddy"
systemctl daemon-reload
systemctl enable "$unit" "$sweep_timer" caddy.service
systemctl restart "$unit"
systemctl restart caddy.service

got=
for _ in $(seq 1 30); do
  got=$(curl -fsS --max-time 2 "$health_url" 2> /dev/null) || got=
  [ "$got" != "$health_answer" ] || break
  sleep 1
done
if [ "$got" != "$health_answer" ]; then
  journalctl -u "$unit" -n 40 --no-pager >&2 || true
  die "$health_url did not answer $health_answer (answered: ${got:-nothing}); the log above is the service's. A KeyError there means $env_file lacks that variable"
fi

# The timer starts after the health check, so that its first sweep does not meet a launcher that is still starting.
systemctl restart "$sweep_timer"

echo "superci: the launcher answers on $health_url; sweeps run every ten minutes; check https://$name/health from another machine"
