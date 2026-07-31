# Options Selling Scanner

A local-first tool that ingests options chains, computes its own greeks and volatility
analytics, scores premium-selling candidates, and renders a dashboard with charts,
levels, and projections.

Currently at **Phase 4**: it pulls real option chains through a provider interface,
lands them on disk as partitioned parquet, runs a daily snapshot job, computes its own
greeks and volatility analytics, ranks premium selling candidates from a YAML
configurable screen, and serves all of it to a browser dashboard. See `DECISIONS.md`
for why things are the way they are and `CLAUDE.md` for the rules any contributor
(human or agent) works under.

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

## Scanning

```
venv\Scripts\python -m optscan scan                      # rank the watchlist
venv\Scripts\python -m optscan scan --explain            # with score breakdowns
venv\Scripts\python -m optscan scan --gaps               # plus flagged mispricings
venv\Scripts\python -m optscan scan --symbol SPY --live  # fresh quotes, one symbol
venv\Scripts\python -m optscan config                    # every knob and its value
```

By default the scan uses the most recent stored snapshot, which is fast, offline, and
reproducible. `--live` fetches fresh chains. Either way the quote age is reported, and
anything over six hours old is called out as stale.

Thresholds live in `screen.yaml`. Every rejection is counted and the top reasons are
printed under the table, so an empty result tells you whether the market was quiet or
your filters were tight.

## Dashboard

```
venv\Scripts\python -m optscan serve
```

Serves the API on http://127.0.0.1:8000, and the dashboard at the same address once the
frontend has been built. Four views: ranked candidates with the score breakdown on
expand, the full chain grid with heatmaps on implied vol and volume, the underlying's
candles and volatility surface, and a payoff diagram for any position you can build
from the stored chain.

Build the frontend once, or run it in dev mode against a live rebuild:

```
npm --prefix frontend install
npm --prefix frontend run build     # then optscan serve hosts it at :8000
npm --prefix frontend run dev       # or Vite on :5173, proxying /api to :8000
```

Two things about the dashboard are deliberate and worth knowing before you read a
number off it:

- **It shows the last stored capture, not the market.** There is no live refresh
  button. The header carries the capture time and its age on every screen, and a
  capture over six hours old is badged stale. Live data arrives in Phase 5 with a
  provider that actually quotes in real time.
- **Two things are fetched at request time, and both say so.** The corporate calendar,
  because without it the dashboard's screen would silently skip the earnings exclusion
  the CLI applies, and the daily candles, because nothing stores them. Either can be
  unavailable, and when it is you get an empty panel with the reason in it rather than
  a blank that looks like a bug.

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

The API tests run through `fastapi.testclient` against the frozen SPY chain in
`tests/fixtures/`, with the provider injected as a fake. No network, no running server,
and no dependency on whatever happens to be in `data/`.

## What the analytics assume

Every module states its own assumptions, and these are the ones that bite:

- Pricing is Black-Scholes-Merton, which is European. Listed equity options are
  American. Calls on non dividend payers agree exactly; puts and dividend paying calls
  are understated, increasingly so deep in the money.
- Probabilities assume lognormal terminal prices at constant volatility. Real returns
  have fatter tails and negative skew, so a strike this tool says has a 90 percent
  chance of expiring worthless is systematically a little optimistic, in exactly the
  scenario a premium seller cares about. Treat them as a ranking device.
- Return on capital uses a stated margin model: cash secured puts are full cash less
  credit, verticals and condors are max loss. Your broker may hold less, which would
  make the reported returns conservative.
- IV rank needs history this tool has to build. Under 20 observations it publishes
  nothing rather than a number, and it labels everything under a year of coverage.
- Candidates are priced at mid. A real fill lands between mid and the far side, so
  every credit here is optimistic by roughly half the spread on each leg.
- Commissions are modelled at 0.65 per contract per leg, in and out, and profit and
  capital are reported in dollars so a spectacular percentage on a tiny base is
  visible for what it is. Change the rates under `costs` in `screen.yaml`.
- The score is a sorting device for a list you then read. It has not been validated
  against outcomes, and the scoring weights and ramps in `screen.yaml` are opinions.
- The gaps module finds candidates for review, not edge. Its thresholds are
  uncalibrated, and one of its four detectors is switched off because it had no
  working baseline.
- The payoff diagram's T+0 curve reprices every leg at the volatility it was entered
  at. A real move, especially a move down, usually comes with a rise in volatility
  that works against a short premium position, so that curve is optimistic in exactly
  the direction that matters.

## What this tool does not do

- It does not place orders. It ranks and displays.
- It does not give advice. A high score is a candidate to review, not a recommendation.
- It does not claim its scores predict anything. Phase 8 exists to test that claim, and
  the honest answer may turn out to be no.
