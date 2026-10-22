# Builds the zip to give someone else a clean copy of the app.
#
#   .\scripts\package_friend.ps1            ->  dist\optscan-friend-<date>-<commit>.zip
#
# ## Allowlist, not blocklist
#
# The zip is built from a short list of the paths the app needs to run, taken from the
# last COMMIT (git archive), never from the working folder. So nothing that is not
# tracked can ship -- the data folder, .env with the Alpaca keys, the sqlite file with
# the broker ledger and journal -- and a personal file added to the repo later does not
# ship either unless someone adds it to the list below on purpose.
#
# ## Then it checks its own work, and refuses on any hit
#
# - No tests\ (a fixture there is a sample of licensed Market Chameleon data), no
#   DECISIONS.md / CLAUDE.md / MAP.md (they describe the owner's own trading), no study
#   results (derived from licensed IV), no database or parquet files, no .env.
# - No text file may contain a personal identifier from $Personal below.
#
# Uncommitted changes are not in the zip; the script says so rather than surprising you.

param([string]$OutDir = "dist")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$Allow = @(
    "src", "pyproject.toml", "constraints.txt", "screen.yaml", "universe.yaml",
    ".env.example", "scripts/optscan.ico", "data/.gitkeep"
)
$Forbidden = @(
    "tests", "DECISIONS.md", "CLAUDE.md", "MAP.md", "map", "docs", "scripts/studies", ".claude", ".env"
)
$ForbiddenPatterns = @("*.sqlite", "*.sqlite-*", "*.parquet", "*marketchameleon*.csv", "*.jsonl")
# Identifiers that must never appear in a shipped file. Lower case; matched case-blind.
# Specific to the owner, not generic phrases: "robinhood account" was tried and fired on
# the importer's docstring describing the export format, which is code, not data.
$Personal = @("kubic", "dominick", "@gmail.com")

$dirty = git status --porcelain -- $Allow packaging/friend
if ($dirty) {
    Write-Host "Note: uncommitted changes under the shipped paths are NOT in the zip:" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "  $_" }
}

Write-Host "Building the dashboard..."
Push-Location frontend
npm run build *> $null
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "The frontend build failed." }
Pop-Location

$commit = (git rev-parse --short HEAD).Trim()
$stage = Join-Path $env:TEMP "optscan-friend-$commit"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null

$archive = Join-Path $env:TEMP "optscan-friend-src-$commit.zip"
git archive --format=zip -o $archive HEAD -- $Allow
if ($LASTEXITCODE -ne 0) { throw "git archive failed." }
Expand-Archive -Path $archive -DestinationPath $stage -Force
Remove-Item $archive

New-Item -ItemType Directory -Path (Join-Path $stage "frontend") -Force | Out-Null
Copy-Item -Recurse "frontend\dist" (Join-Path $stage "frontend\dist")
git archive --format=zip -o $archive HEAD -- packaging/friend
Expand-Archive -Path $archive -DestinationPath (Join-Path $env:TEMP "optscan-friend-pk-$commit") -Force
Copy-Item (Join-Path $env:TEMP "optscan-friend-pk-$commit\packaging\friend\*") $stage -Force
Remove-Item -Recurse -Force (Join-Path $env:TEMP "optscan-friend-pk-$commit"), $archive

# ---- checks ----
$problems = @()
foreach ($path in $Forbidden) {
    if (Test-Path (Join-Path $stage $path)) { $problems += "forbidden path shipped: $path" }
}
foreach ($pattern in $ForbiddenPatterns) {
    Get-ChildItem -Path $stage -Recurse -Filter $pattern -File | ForEach-Object {
        $problems += "forbidden file shipped: $($_.FullName.Substring($stage.Length + 1))"
    }
}
$textFiles = Get-ChildItem -Path $stage -Recurse -File |
    Where-Object { $_.Extension -in ".py", ".md", ".yaml", ".toml", ".txt", ".ps1", ".cmd", ".example", ".json", ".html", ".js", ".css" }
foreach ($file in $textFiles) {
    $text = (Get-Content $file.FullName -Raw -ErrorAction SilentlyContinue)
    if (-not $text) { continue }
    $lower = $text.ToLower()
    foreach ($needle in $Personal) {
        if ($lower.Contains($needle)) {
            $problems += "personal identifier '$needle' in $($file.FullName.Substring($stage.Length + 1))"
        }
    }
}
if ($problems) {
    $problems | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Remove-Item -Recurse -Force $stage
    throw "Refusing to build: $($problems.Count) problem(s) above."
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$zip = Join-Path $OutDir ("optscan-friend-{0}-{1}.zip" -f (Get-Date -Format "yyyyMMdd"), $commit)
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip
Remove-Item -Recurse -Force $stage
$size = [Math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Built $zip ($size MB) from commit $commit. Checks passed." -ForegroundColor Green
