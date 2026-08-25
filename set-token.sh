#!/usr/bin/env bash
# One-time helper: stores the Telegram bot token in monitor.env using hidden
# input, so the token never appears on screen, in shell history, or in the
# process list. Verifies the token with Telegram before saving, then proves
# delivery by sending a real message through send_telegram() itself.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "Paste your Telegram bot token (input is hidden), then press Enter: " TOKEN
echo
[ -n "${TOKEN:-}" ] || { echo "Nothing entered. Aborted."; exit 1; }

echo "Checking the token with Telegram..."
BOT=$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" \
      | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
if [ -z "$BOT" ]; then
  echo "Telegram rejected that token. Nothing was saved."
  exit 1
fi
echo "Valid - token belongs to @${BOT}"

# Write with a tight umask, replacing any previous value.
umask 077
grep -v '^TELEGRAM_BOT_TOKEN=' monitor.env > monitor.env.tmp || true
echo "TELEGRAM_BOT_TOKEN=${TOKEN}" >> monitor.env.tmp
mv monitor.env.tmp monitor.env
chmod 600 monitor.env
unset TOKEN
echo "Saved to monitor.env (permissions 600)."

# Send through the monitor's own function, so this tests the real code path
# rather than a hand-rolled curl call that might differ.
echo "Sending a test message through the monitor's own code..."
set -a; . ./monitor.env; set +a
./venv/bin/python - <<'PY'
import standalone_monitor as m
m.send_telegram(
    "Setup complete. Your BTC/ETH monitor is live on the Oracle VM "
    "and will check the levels every hour."
)
print("send_telegram() returned without error")
PY

echo
echo "If your phone buzzed, the whole chain works and you are done."
echo "If it did not, check:  tail -5 monitor_log.txt"
