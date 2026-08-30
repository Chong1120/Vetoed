#!/usr/bin/env bash
#
# Provision Vetoed on a fresh Ubuntu VPS. Idempotent - safe to re-run.
#
#   sudo bash deploy/install.sh
#
# Deliberately does NOT start the agent. It stops after installing the units
# and tells you to write /opt/vetoed/.env, because starting a trading agent as
# a side effect of an install script is not a thing that should happen.

set -euo pipefail

APP_USER=vetoed
APP_DIR=/opt/vetoed
REPO_URL="${REPO_URL:-https://github.com/Chong1120/Vetoed.git}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

say "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

say "Service user ${APP_USER}"
# --system: no login shell, no password, not a person. The agent holds broker
# credentials, so it should not be an account anyone can log in as.
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

say "Code at ${APP_DIR}"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin
    git -C "$APP_DIR" reset --hard --quiet origin/main
else
    mkdir -p "$APP_DIR"
    git clone --quiet "$REPO_URL" "$APP_DIR"
fi

say "Virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
# The trading agent plus the dashboard extra; the dashboard unit is optional
# but the health endpoint is worth having.
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR[dashboard]"

say "Journal directory"
mkdir -p "$APP_DIR/journal"

say "Environment file"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    warn "created $APP_DIR/.env from the template - EDIT IT before starting"
fi
# 600 and owned by the service user. The unit file is world-readable; this is
# not, which is why the keys live here and not in the unit.
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "systemd units"
install -m 644 "$APP_DIR/deploy/vetoed.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/vetoed-dashboard.service" /etc/systemd/system/
systemctl daemon-reload

say "Smoke test"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pytest -q "$APP_DIR/tests" \
    || warn "tests failed - do not start the agent until this is understood"

cat <<'NEXT'

Installed. NOT started - three things left, in this order:

  1. Put your Alpaca PAPER keys in /opt/vetoed/.env
         sudo -u vetoed nano /opt/vetoed/.env
     ALPACA_PAPER_TRADE must be exactly: true
     The agent refuses to start otherwise, and that check is not overridable.

  2. Prove it works for one cycle before letting it run unattended:
         cd /opt/vetoed
         sudo -u vetoed .venv/bin/python -m agent.loop --force
     That is a DRY RUN (no --live): it screens, decides, and journals, but
     submits nothing. Read the output before continuing.

  3. Enable and start:
         sudo systemctl enable --now vetoed
         sudo systemctl status vetoed
         journalctl -u vetoed -f

  Optional read-only dashboard on 127.0.0.1:8000:
         sudo systemctl enable --now vetoed-dashboard
         ssh -N -L 8000:127.0.0.1:8000 you@this-host

NEXT
