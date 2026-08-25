#!/usr/bin/env bash
# Hourly wrapper for cron. Sourcing monitor.env keeps the bot token out of
# the crontab and out of the process list.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./monitor.env; set +a
exec ./venv/bin/python standalone_monitor.py
