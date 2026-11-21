#!/usr/bin/env bash
# Runs ON the VM (Debian/Ubuntu). Expects monitor.py, requirements.txt and .env
# to have been copied to ~/housing-monitor first. Idempotent: rerun to update.
set -euo pipefail

APP_DIR="$HOME/housing-monitor"
cd "$APP_DIR"

if ! command -v python3 >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-venv
fi

[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
chmod 600 .env

sudo tee /etc/systemd/system/housing-monitor.service >/dev/null <<EOF
[Unit]
Description=Kleinanzeigen Berlin housing alerts to Telegram
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -u $APP_DIR/monitor.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now housing-monitor
sudo systemctl restart housing-monitor
sleep 5
systemctl --no-pager --lines=15 status housing-monitor
