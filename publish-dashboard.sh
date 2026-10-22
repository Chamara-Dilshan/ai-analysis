#!/usr/bin/env bash
# Publish the PUBLIC dashboard - the paper record only, never real trades - to
# GitHub Pages. Runs daily from cron on the VM.
#
# It pushes to a separate repo that holds nothing but this page, using a deploy
# key with write access to THAT repo only (~/.ssh/dashboard_deploy). The VM's
# key for the code repo stays read-only, so this job can never push code.
#
# Each run force-pushes a single fresh commit: the site has no history to grow,
# and yesterday's page is simply replaced.
set -euo pipefail
cd "$(dirname "$0")"
site="$HOME/dashboard-site"
key="$HOME/.ssh/dashboard_deploy"
remote="${DASHBOARD_REMOTE:-git@github.com:Chamara-Dilshan/ai-analysis-dashboard.git}"
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

rm -rf "$site"
mkdir -p "$site"
./venv/bin/python -c "import standalone_monitor as m; m.write_public_dashboard('$site/index.html')"
touch "$site/.nojekyll"      # serve the file as-is, no Jekyll processing

cd "$site"
git init -q -b main
git add -A
git -c user.name="trading-monitor" -c user.email="monitor@users.noreply.github.com" \
    commit -q -m "Dashboard $(date -u +%Y-%m-%d)"
git push -q -f "$remote" main
echo "dashboard: published $(date -u +%Y-%m-%dT%H:%MZ)"
