#!/usr/bin/env bash
# Pull the latest monitor code and prove it still runs. Replaces the old
# scp-from-the-laptop flow now that the VM is a real clone.
#
#   ./update.sh            deploy origin/main
#   ./update.sh <commit>   deploy (or roll back to) that commit
#
# The test suite runs on the candidate commit in a throwaway worktree BEFORE
# the live checkout moves, so a failing commit never reaches the hourly cron
# — not even for the seconds a test run takes.
set -euo pipefail
cd "$(dirname "$0")"
git fetch -q origin
target=$(git rev-parse --verify "${1:-origin/main}^{commit}")
current=$(git rev-parse HEAD)
repo=$(pwd)

check=$(mktemp -d)
trap 'git worktree remove --force "$check" 2>/dev/null || true; rm -rf "$check"' EXIT
git worktree add -q --detach "$check" "$target"
# The candidate's own requirements, so a dependency bump is tested too.
./venv/bin/pip install -q -r "$check/requirements-dev.txt"
echo "--- testing $(git log --oneline -1 "$target") ---"
if ! (cd "$check" && "$repo/venv/bin/python" -m pytest -q -p no:cacheprovider); then
    echo "!!! TESTS FAILED — not deploying. Still on $(git log --oneline -1 "$current")"
    exit 1
fi

git reset -q --hard "$target"
echo "--- now on $(git log --oneline -1) (was ${current:0:7}) ---"
./run-monitor.sh

# The Telegram bot is a long-running service, so new code only takes effect
# once it restarts. Skipped where the service is not installed.
if systemctl list-unit-files trading-bot.service >/dev/null 2>&1; then
    sudo systemctl restart trading-bot && echo "--- trading-bot restarted ---"
fi
