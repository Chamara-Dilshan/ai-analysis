# Syncs the monitor source (this folder) to the Oracle Cloud VM that runs the
# always-on hourly check. The VM sits in Mumbai because Binance answers Indian
# IPs and refuses US ones with HTTP 451 -- see the "monitor-off-laptop-migration"
# memory for why GitHub Actions could not host this.
#
# The VM copy is NOT a git clone, so this script is how code changes get there.
# monitor.env (the bot token) lives only on the VM and is never touched here.
#
# Usage: powershell -File sync-to-vm.ps1
param(
    [switch]$NoPause,
    [string]$VmIp = "144.24.112.25"
)

$ErrorActionPreference = "Stop"
$SRC = $PSScriptRoot
$KEY = "C:\Users\chama\.ssh\oracle-btc-monitor.key"
$REMOTE = "ubuntu@$VmIp"
$REMOTE_DIR = "~/trading-analysis"

function Finish($code) {
    if (-not $NoPause) { Read-Host "Press Enter to close" }
    exit $code
}

Write-Host "=== Syncing monitor to Oracle VM ===" -ForegroundColor Cyan
Write-Host "  source: $SRC"
Write-Host "  target: $REMOTE`:$REMOTE_DIR"
Write-Host ""

if (-not (Test-Path $KEY)) {
    Write-Host "SSH key not found: $KEY" -ForegroundColor Red
    Write-Host "The public IP is ephemeral -- if the instance was stopped and started," -ForegroundColor Yellow
    Write-Host "pass the new one: powershell -File sync-to-vm.ps1 -VmIp <new-ip>" -ForegroundColor Yellow
    Finish 1
}

# Run the local test suite first. A broken monitor that runs hourly and silently
# logs errors is exactly the failure this whole migration was meant to end.
Write-Host "=== Running local test suite first ===" -ForegroundColor Cyan
$localPy = "C:\Users\chama\mcp-servers\binance-btc-futures\venv\Scripts\python.exe"
if (Test-Path $localPy) {
    Push-Location $SRC
    & $localPy -m pytest -q
    $testExit = $LASTEXITCODE
    Pop-Location
    if ($testExit -ne 0) {
        Write-Host ""
        Write-Host "TESTS FAILED (exit $testExit) -- not syncing." -ForegroundColor Red
        Finish $testExit
    }
    Write-Host "Tests passed." -ForegroundColor Green
} else {
    Write-Host "Local venv python not found, skipping tests." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Copying files ===" -ForegroundColor Cyan
$files = @("server.py", "structure.py", "risk.py", "news.py",
           "standalone_monitor.py", "requirements.txt")
$paths = @()
foreach ($f in $files) {
    $p = Join-Path $SRC $f
    if (Test-Path $p) { $paths += $p; Write-Host "  $f" }
}

& scp -i $KEY -o StrictHostKeyChecking=no -q @paths "$REMOTE`:$REMOTE_DIR/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "scp failed (exit $LASTEXITCODE)." -ForegroundColor Red
    Finish $LASTEXITCODE
}

# Dependencies may have changed; reinstall is cheap and idempotent.
Write-Host ""
Write-Host "=== Updating dependencies and running one check ===" -ForegroundColor Cyan
& ssh -i $KEY -o StrictHostKeyChecking=no $REMOTE `
    "cd trading-analysis && ./venv/bin/pip install -q -r requirements.txt && ./run-monitor.sh"
$runExit = $LASTEXITCODE

Write-Host ""
if ($runExit -eq 0) {
    Write-Host "Sync complete and the monitor ran cleanly on the VM." -ForegroundColor Green
    Write-Host "Hourly cron picks up the new code automatically -- nothing to restart." -ForegroundColor Green
} else {
    Write-Host "The monitor FAILED on the VM (exit $runExit)." -ForegroundColor Red
    Write-Host "Check it directly:  ssh -i $KEY $REMOTE" -ForegroundColor Yellow
    Write-Host "Then read:  tail -40 ~/trading-analysis/monitor_log.txt" -ForegroundColor Yellow
}

Finish $runExit
