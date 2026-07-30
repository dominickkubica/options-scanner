# Options Selling Scanner

A local-first tool that ingests options chains, computes its own greeks and volatility
analytics, scores premium-selling candidates, and renders a dashboard with charts,
levels, and projections.

Currently at **Phase 1**: it pulls real option chains through a provider interface and
lands them on disk as partitioned parquet, and the daily snapshot job is running. No
analytics yet. See `DECISIONS.md` for why things are the way they are and `CLAUDE.md`
for the rules any contributor (human or agent) works under.

## Start the snapshot job today

This is the one thing that cannot wait. IV rank and IV percentile are the core signal
for premium selling, they need months of history, and no free source will sell you that
history after the fact. Every day the job does not run is a permanent hole.

```
venv\Scripts\python -m optscan schedule --install
```

Registers a Windows scheduled task that runs `optscan snapshot` daily. The capture time
is defined in market local time (15:45 New York by default) and converted to your
machine's clock. Nothing consumes the output until Phase 2, and that is fine.

## Setup

```
py -3.12 -m venv venv
venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
venv\Scripts\pre-commit install
```

## Run

```
venv\Scripts\python -m optscan status              # market state, watchlist, recent captures
venv\Scripts\python -m optscan snapshot            # capture the watchlist now
venv\Scripts\python -m optscan snapshot --force    # capture with the market closed, stale mark
venv\Scripts\python -m optscan watchlist add TSLA
venv\Scripts\python -m optscan schedule            # print the scheduled task command
venv\Scripts\python -m optscan --check             # print resolved config, touch nothing
```

The job declines to capture outside a trading session, because a pre open capture would
file yesterday's close under today's session date. `--force` overrides that and labels
the result honestly. Re-running on the same day is a no op unless you pass `--recapture`.

## Reading what it captured

```python
from optscan.config import Settings
from optscan.storage import read_snapshots

df = read_snapshots(Settings().snapshot_path, symbol="SPY")
```

One row per contract. Date columns come back as pandas datetime64, and timestamps are
UTC regardless of the machine's timezone. Files live at
`data/snapshots/symbol=SPY/session_date=2026-07-30/<capture time>.parquet`.

Roughly 600 KB per day for six liquid symbols, so about 150 MB a year.

## Develop

```
venv\Scripts\python -m pytest
venv\Scripts\python -m ruff check .
venv\Scripts\python -m ruff format .
```

## What this tool does not do

- It does not place orders. It ranks and displays.
- It does not give advice. A high score is a candidate to review, not a recommendation.
- It does not claim its scores predict anything. Phase 8 exists to test that claim, and
  the honest answer may turn out to be no.
