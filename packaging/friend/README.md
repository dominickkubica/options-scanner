# optscan: an options scanner and trade-idea finder

A dashboard that runs on your own computer. It finds stocks hitting a tested
oversold-bounce signal, shows support and resistance levels, expected-move projections,
option chains, gamma and open-interest levels, and ranks credit spreads.

Everything runs locally. Your data lives in the `data` folder next to this file and is
never sent anywhere except the market-data services the app reads from (Yahoo Finance,
and Alpaca if you add a key).

## Install (Windows)

1. Unzip this folder somewhere permanent, for example `Documents\optscan`.
2. Double-click **`Install optscan.cmd`** and answer the questions.
3. Open the **optscan dashboard** shortcut on your desktop.

The installer needs no admin rights. It uses Python 3.12 if you have it, and otherwise
offers to install `uv` (a small tool from astral.sh) to provide one.

### The free Alpaca key (recommended)

Trade ideas need ten years of daily prices for a few hundred stocks. The app gets them
from Alpaca's free plan. Sign up at https://app.alpaca.markets/signup, open the paper
trading account, choose *API Keys -> Generate*, and paste both keys when the installer
asks. Without a key, everything except trade ideas and signals still works.

### The daily jobs

The app gets better over time by taking a snapshot of option prices at 15:45 New York
time each trading day. The installer can register this with Windows Task Scheduler; the
computer needs to be on and logged in at that time. Missed days are not a problem, they
just leave a gap.

## What to look at

- **Trade ideas** (sidebar): stocks that closed oversold (RSI at or below 30). Holding
  the stock for five days after that signal is the one strategy here that has passed an
  out-of-sample test. Each idea also shows a call debit spread to express it with:
  strikes, expiry, a limit price, a walk-away price, and when to close.
- **A ticker's Key levels tab**: price levels that have actually held, the expected
  move to each expiry, and open interest walls, max pain and the gamma regime.
- **Best plays / Options chain**: credit spreads ranked by a composite score.

## Read this before trading anything

- **None of this is financial advice.** It is a research tool that shows its working.
- **The call debit spread plan is a lead, not a proven trade.** The bounce is proven on
  the stock. The spread version looked strong in testing but missed the bar set before
  the test, so treat it as something to try small and track. The plan says so on screen.
- **Your fill price decides whether the spread makes money.** In testing, paying 5%
  above the mid price kept most of the edge; paying 15% above it lost money. Use the
  limit and walk-away prices the plan gives you.
- **The composite score on Best plays is a sorting aid.** It has not been shown to
  predict which trades do better.
- **Open-interest levels describe the previous close** and are not tested predictors.

## Updating

Ask for a new zip, unzip it over this folder (keep your `data` folder and `.env`), and
run `Install optscan.cmd` again. It reuses everything already installed.

## If something goes wrong

- Nothing opens: run `venv\Scripts\optscan serve` from this folder and read the error.
- Trade ideas empty: the Alpaca key is missing, or prices have not been downloaded yet;
  run `venv\Scripts\optscan prices sync --provider alpaca`.
- To remove it: run `venv\Scripts\optscan shortcut --remove`; if you set up the daily
  jobs, remove them in PowerShell with
  `Get-ScheduledTask -TaskName 'Optscan*' | Unregister-ScheduledTask -Confirm:$false`;
  then delete this folder.
