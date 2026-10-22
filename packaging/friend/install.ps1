# Installs the options scanner on this computer, for this user only.
#
# Double-click "Install optscan.cmd" rather than running this directly. Nothing here
# needs administrator rights, nothing is installed outside this folder except the
# desktop shortcut, and nothing about you is sent anywhere: your data stays in .\data.
#
# Options (mostly for testing):
#   -PythonExe C:\path\to\python.exe   use this Python 3.12 instead of looking for one
#   -AlpacaKey KEY -AlpacaSecret SECRET  skip the key prompt
#   -NonInteractive                    never prompt; skip anything that would need to
#   -SkipSync -NoShortcut -NoSchedule -NoLaunch   skip that step

param(
    [string]$PythonExe = "",
    [string]$AlpacaKey = "",
    [string]$AlpacaSecret = "",
    [switch]$NonInteractive,
    [switch]$SkipSync,
    [switch]$NoShortcut,
    [switch]$NoSchedule,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ask($question, $default = "y") {
    if ($NonInteractive) { return ($default -eq "y") }
    $answer = Read-Host "$question [$default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $default }
    return $answer.Trim().ToLower().StartsWith("y")
}

# ---------------------------------------------------------------------------------
Step "1/6  Python 3.12"
# The app is pinned to Python 3.12. Use one that is already installed if there is one,
# otherwise install uv (a single small program from astral.sh) and let it fetch 3.12
# into its own cache. Either way nothing system-wide changes.
$venvPython = Join-Path $here "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $base = $PythonExe
    if (-not $base) {
        try {
            $found = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $base = $found.Trim() }
        } catch {}
    }
    if ($base) {
        Write-Host "Using $base"
        & $base -m venv venv
    } else {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) {
            Write-Host "No Python 3.12 found. Installing uv from astral.sh to provide one."
            if (-not (Ask "Download and install uv now?")) { throw "Python 3.12 is required." }
            powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
            $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        }
        & uv venv --python 3.12 venv
        if ($LASTEXITCODE -ne 0) { throw "uv could not create the environment." }
        & uv pip install --python $venvPython pip | Out-Null
    }
    if (-not (Test-Path $venvPython)) { throw "Creating the Python environment failed." }
} else {
    Write-Host "Environment already exists; reusing it."
}

Step "2/6  Installing the app (a few minutes the first time)"
& $venvPython -m pip install --disable-pip-version-check -q --upgrade pip
& $venvPython -m pip install --disable-pip-version-check -q -e . -c constraints.txt
if ($LASTEXITCODE -ne 0) { throw "Installing the app's packages failed. See the messages above." }
$optscan = Join-Path $here "venv\Scripts\optscan.exe"

# ---------------------------------------------------------------------------------
Step "3/6  Settings"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
$envText = Get-Content ".env" -Raw
if ($envText -notmatch "OPTSCAN_ALPACA_KEY_ID") {
    Write-Host @"
Trade ideas need about ten years of daily prices for a few hundred stocks. The free
source the app uses for that is Alpaca. A free account takes two minutes:
  https://app.alpaca.markets/signup  ->  then "API Keys" -> Generate, on the paper account.
The keys stay in this folder's .env file and are only ever sent to Alpaca.
Without them everything else works: charts, key levels, projections, option chains.
"@
    if (-not $AlpacaKey -and -not $NonInteractive) {
        $AlpacaKey = Read-Host "Alpaca API key ID (leave blank to skip)"
        if ($AlpacaKey) { $AlpacaSecret = Read-Host "Alpaca secret key" }
    }
    if ($AlpacaKey -and $AlpacaSecret) {
        Add-Content ".env" "`nOPTSCAN_ALPACA_KEY_ID=$AlpacaKey`nOPTSCAN_ALPACA_SECRET_KEY=$AlpacaSecret"
        Write-Host "Saved."
    } else {
        Write-Host "Skipped. Re-run this installer any time to add them."
    }
}
$hasKeys = (Get-Content ".env" -Raw) -match "OPTSCAN_ALPACA_KEY_ID="

# Touching the watchlist creates the database and seeds the default list.
& $optscan watchlist list | Out-Null

# ---------------------------------------------------------------------------------
Step "4/6  Price history"
if ($SkipSync) {
    Write-Host "Skipped (-SkipSync)."
} elseif (-not $hasKeys) {
    Write-Host "Skipped: needs the Alpaca keys from step 3. Trade ideas stay empty until then."
} elseif (Ask "Download ten years of daily prices now? (5-15 minutes)") {
    & $optscan prices sync --provider alpaca
}

# ---------------------------------------------------------------------------------
Step "5/6  Desktop shortcut"
if ($NoShortcut) { Write-Host "Skipped." } else { & $optscan shortcut }

Step "6/6  Daily jobs"
Write-Host @"
The app learns from a snapshot of option prices taken every trading day at 15:45
New York time, and refreshes prices and ideas after the close. Windows can run these
for you while you are logged in. They only touch this folder.
"@
if ($NoSchedule) {
    Write-Host "Skipped."
} elseif (Ask "Set up the daily jobs in Windows Task Scheduler?") {
    # The default three protect history that cannot be rebuilt. With a key, the price
    # refresh and the signal scan are added, or trade ideas go stale after the first
    # day: the ideas list skips any symbol whose last bar is old.
    if ($hasKeys) {
        & $optscan schedule --install snapshot record resolve prices signals
    } else {
        & $optscan schedule --install snapshot record resolve
    }
} else {
    Write-Host "Skipped. Run  venv\Scripts\optscan schedule --install  later to add them."
}

Write-Host ""
Write-Host "Installed. Open it from the 'optscan dashboard' desktop shortcut." -ForegroundColor Green
if (-not $NoLaunch -and -not $NonInteractive) {
    Start-Process -FilePath $optscan -ArgumentList "serve" -WorkingDirectory $here
    Start-Sleep -Seconds 4
    Start-Process "http://127.0.0.1:8000"
}
