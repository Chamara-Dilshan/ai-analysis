#!/usr/bin/env bash
# systemd entry point for bot.py. Sourcing monitor.env keeps the bot token
# out of the unit file and the process list, same as run-monitor.sh.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./monitor.env; set +a
exec ./venv/bin/python bot.py
