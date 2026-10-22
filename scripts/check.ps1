# Lint, format and test in one command, printing only what needs reading.
#
#   .\scripts\check.ps1          # fix what ruff can, then run the suite
#   .\scripts\check.ps1 -Quick   # one test file or pattern: -Quick tests\test_gex.py
#
# The full output of each step is kept in data\logs\check-*.log, so nothing is lost by
# not printing it: the terminal gets the summary, the file has the detail.

param([string]$Quick = "")

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$failed = $false

Push-Location $root
try {
    & $python -m ruff check --fix src tests scripts *> (Join-Path $logs "check-$stamp-ruff.log")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ruff: problems ruff could not fix:" -ForegroundColor Yellow
        Get-Content (Join-Path $logs "check-$stamp-ruff.log") |
            Select-String -Pattern "^[A-Z]+[0-9]+ |-->" | Select-Object -First 20 |
            ForEach-Object { Write-Host "  $_" }
        $failed = $true
    } else {
        Write-Host "ruff: clean" -ForegroundColor Green
    }

    & $python -m ruff format src tests scripts *> (Join-Path $logs "check-$stamp-format.log")
    $formatted = Get-Content (Join-Path $logs "check-$stamp-format.log") | Select-Object -Last 1
    Write-Host "format: $formatted"

    $target = if ($Quick) { $Quick } else { "tests" }
    $testLog = Join-Path $logs "check-$stamp-pytest.log"
    & $python -m pytest $target -p no:cacheprovider *> $testLog
    if ($LASTEXITCODE -ne 0) { $failed = $true }
    Get-Content $testLog | Select-String -Pattern "^(FAILED|ERROR) " | Select-Object -First 20 |
        ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    $summary = Get-Content $testLog | Select-String -Pattern "passed|failed|error" |
        Select-Object -Last 1
    $colour = if ($failed) { "Red" } else { "Green" }
    Write-Host "pytest: $summary" -ForegroundColor $colour
    if ($failed) { Write-Host "full output: $logs\check-$stamp-*.log" }
} finally {
    Pop-Location
}
if ($failed) { exit 1 }
