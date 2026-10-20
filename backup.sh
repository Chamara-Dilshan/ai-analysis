#!/usr/bin/env bash
# Daily snapshot of the runtime files git does not track. paper_trades.json
# is the one forward record no backtest bug can contaminate, and until now it
# lived only on this VM's single disk.
#
#   ./backup.sh          snapshot; also send it to Telegram on Sundays (UTC)
#   ./backup.sh --send   snapshot and send it to Telegram now
#
# Local copies (last 30) cover a corrupted or mistakenly edited file. The
# weekly Telegram copy is the off-VM one: it covers losing the VM itself.
# Restore: tar -xzf ~/backups/monitor-YYYY-MM-DD.tar.gz -C ~/trading-analysis
set -euo pipefail
cd "$(dirname "$0")"
dest="$HOME/backups"
mkdir -p "$dest"

files=()
for f in paper_trades.json positions.json monitor_state.json; do
    [ -f "$f" ] && files+=("$f")
done
archive="$dest/monitor-$(date -u +%Y-%m-%d).tar.gz"
tar -czf "$archive" "${files[@]}"
ls -1t "$dest"/monitor-*.tar.gz | tail -n +31 | xargs -r rm --
echo "backup: $archive (${files[*]})"

if [ "$(date -u +%u)" = 7 ] || [ "${1:-}" = "--send" ]; then
    set -a; . ./monitor.env; set +a
    trades=$(./venv/bin/python -c "import json;print(len(json.load(open('paper_trades.json'))))" 2>/dev/null || echo "?")
    # The URL (with the token) goes in via stdin, never argv, so it stays
    # out of the process list — same reason run-monitor.sh sources monitor.env.
    if printf 'url = "https://api.telegram.org/bot%s/sendDocument"\n' "$TELEGRAM_BOT_TOKEN" \
        | curl -sS -f -o /dev/null --max-time 30 -K - \
            -F chat_id="$TELEGRAM_CHAT_ID" \
            -F document=@"$archive" \
            -F caption="Weekly monitor backup — $trades paper trades. Keep this file; it restores the journal if the VM is lost."; then
        echo "backup: sent to Telegram"
    else
        echo "backup: TELEGRAM SEND FAILED (local copy kept)"
        exit 1
    fi
fi
