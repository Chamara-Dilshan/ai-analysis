#!/usr/bin/env bash
# Pull the latest monitor code and prove it still runs. Replaces the old
# scp-from-the-laptop flow now that the VM is a real clone.
set -euo pipefail
cd "$(dirname "$0")"
git fetch -q origin
git reset --hard origin/main
./venv/bin/pip install -q -r requirements.txt
echo "--- now on $(git log --oneline -1) ---"
./run-monitor.sh
