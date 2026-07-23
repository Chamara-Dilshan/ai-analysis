# Syncs the source copy (this folder) to the live copy Claude Desktop actually
# runs (C:\Users\chama\mcp-servers\binance-btc-futures — a separate, non-git
# directory), then runs the live test suite so a broken sync is caught before
# you restart Desktop. See Docs memory "claude-desktop-strips-mcpservers" for
# why the two copies exist.
#
# Usage: double-click sync-to-live.cmd, or run this directly:
#   powershell -File sync-to-live.ps1
param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$SRC  = $PSScriptRoot
$LIVE = "C:\Users\chama\mcp-servers\binance-btc-futures"

function Finish($code) {
    if (-not $NoPause) { Read-Host "Press Enter to close" }
    exit $code
}

Write-Host "=== Syncing MCP server ===" -ForegroundColor Cyan
Write-Host "  source: $SRC"
Write-Host "  live:   $LIVE"
Write-Host ""

if (-not (Test-Path $LIVE)) {
    Write-Host "Live directory not found: $LIVE" -ForegroundColor Red
    Finish 1
}

# Back up the live server.py before overwriting -- the live dir isn't
# git-tracked, so this .bak is its only rollback point.
if (Test-Path "$LIVE\server.py") {
    Copy-Item "$LIVE\server.py" "$LIVE\server.py.bak" -Force
}

foreach ($f in @("server.py", "risk.py", "news.py", "structure.py", "README.md")) {
    $srcFile = Join-Path $SRC $f
    if (Test-Path $srcFile) {
        Copy-Item $srcFile (Join-Path $LIVE $f) -Force
        Write-Host "  copied $f"
    }
}

$testDir = Join-Path $LIVE "tests"
if (-not (Test-Path $testDir)) {
    New-Item -ItemType Directory -Path $testDir | Out-Null
}
Get-ChildItem (Join-Path $SRC "tests") -Filter "test_*.py" | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $testDir $_.Name) -Force
    Write-Host "  copied tests\$($_.Name)"
}

Write-Host ""
Write-Host "=== Running live test suite ===" -ForegroundColor Cyan
$py = Join-Path $LIVE "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Live venv python not found at $py" -ForegroundColor Red
    Finish 1
}

Push-Location $LIVE
& $py -m pytest -q
$testExit = $LASTEXITCODE
Pop-Location

Write-Host ""
if ($testExit -eq 0) {
    Write-Host "All tests passed." -ForegroundColor Green
    Write-Host "Now FULLY QUIT and reopen Claude Desktop to load the changes." -ForegroundColor Green
} else {
    Write-Host "TESTS FAILED (exit $testExit) -- do NOT restart Desktop until this is fixed." -ForegroundColor Red
    Write-Host "The previous server.py is saved at $LIVE\server.py.bak if you need to roll back." -ForegroundColor Yellow
}

Finish $testExit
