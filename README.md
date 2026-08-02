# Options Selling Scanner

A local-first tool that ingests options chains, computes its own greeks and volatility
analytics, scores premium-selling candidates, tracks the positions you actually hold,
and logs every candidate it surfaces so the score can eventually be tested against what
happened.

It runs on one machine, stores everything on disk, and places no orders.

Phases 0 to 9 are complete. `DECISIONS.md` records why things are the way they are, and
`CLAUDE.md` carries the rules any contributor, human or agent, works under.

---

## Start the daily jobs today

This is the one thing that cannot wait, and it is the first thing to do after setup.

IV rank and IV percentile are the core signal for premium selling. They need months of
history, and no free source will sell you that history after the fact. The validation
study has the same property: a candidate that was never logged when it was scored cannot
be settled later, and there is no way to reconstruct what the screen would have surfaced
last Tuesday. **Every day the jobs do not run is a permanent hole.**

```bash
venv\Scripts\python -m optscan schedule
```

That prints every recurring job, when it runs, and whether Windows currently has it.
Then install them:

```bash
venv\Scripts\python -m optscan schedule --install
```

| Job | Market time | What it protects |
|---|---|---|
| `snapshot` | 15:45 | The IV history. Cannot be bought after the fact. |
| `record` | 16:15 | The validation study. Runs after the capture because it scores it. |
| `backup` | 16:30 | Both of the above. |
| `resolve` | 08:00 | Settles expiries that have finished, from the underlying close. |
| `manage` | every 15 min | Marks positions and fires alerts. **Not installed by default.** |

`manage` is left out of the default install on purpose. At a quarter hour interval it
hits the provider about 26 times a session, yfinance publishes no rate limit and
throttles silently, and being throttled at 15:45 costs a capture that cannot be rebuilt.
Install it explicitly, once the alerts go somewhere you actually read:

```bash
venv\Scripts\python -m optscan schedule --install manage
```

The tasks are registered with `StartWhenAvailable` set and the battery restrictions
cleared, so a machine that was asleep runs the job late rather than skipping it. A late
capture is a slightly worse observation, because IV rank wants a consistent time of day.
A missing one is invisible and permanent.

---

## Setup

```bash
py -3.12 -m venv venv
venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
venv\Scripts\pre-commit install
npm --prefix frontend install
npm --prefix frontend run build
```

Python 3.12 specifically: the venv is pinned to it and the system default on the
development machine is 3.13. Use `venv\Scripts\python` for everything.

Nothing needs credentials. yfinance is the default provider and takes none. A free
Tradier sandbox token from developer.tradier.com enables the live feed, and it is the
one thing this project cannot set up for itself: paste it into the empty
`OPTSCAN_TRADIER_TOKEN=` line in `.env`.

Every setting lives in `.env.example` with the reasoning next to it. Screen thresholds
are separate, in `screen.yaml`.

---

## Run

```bash
venv\Scripts\python -m optscan status              # market state, watchlist, recent captures
venv\Scripts\python -m optscan snapshot            # capture the watchlist now
venv\Scripts\python -m optscan snapshot --force    # capture with the market closed, stale mark
venv\Scripts\python -m optscan watchlist add TSLA
venv\Scripts\python -m optscan --check             # print resolved config, touch nothing
```

The job declines to capture outside a trading session, because a pre-open capture would
file yesterday's close under today's session date. `--force` overrides that and labels
the result honestly. Re-running on the same day is a no-op unless you pass `--recapture`.

### Scanning

```bash
venv\Scripts\python -m optscan scan                      # rank the watchlist
venv\Scripts\python -m optscan scan --explain            # with score breakdowns
venv\Scripts\python -m optscan scan --gaps               # plus flagged mispricings
venv\Scripts\python -m optscan scan --symbol SPY --live  # fresh quotes, one symbol
venv\Scripts\python -m optscan config                    # every knob and its value
```

By default the scan uses the most recent stored snapshot, which is fast, offline, and
reproducible. `--live` fetches fresh chains. Either way the quote age is reported, and
anything over six hours old is called out as stale.

Every rejection is counted and the top reasons are printed under the table, so an empty
result tells you whether the market was quiet or your filters were tight.

### Positions

```bash
venv\Scripts\python -m optscan position add SPY --expiry 2026-09-18 --leg sell:P:700:5.20
venv\Scripts\python -m optscan position list
venv\Scripts\python -m optscan manage                    # mark, evaluate triggers, alert once
venv\Scripts\python -m optscan position close 1 --value -180
```

The fill price is required on every leg. It is the one number the tool cannot
reconstruct and the one every profit figure is measured against.

Short legs are marked at the ask, not the mid, because the ask is what it costs to
close. A short book marked at the mid is overstated by half the spread on every leg,
which on wide contracts is most of the last of the credit, and that is exactly where a
profit target fires.

### Validation

```bash
venv\Scripts\python -m optscan record      # log every candidate the screen surfaces
venv\Scripts\python -m optscan resolve     # settle candidates whose expiry has passed
venv\Scripts\python -m optscan validate    # the report, or a refusal to give one
```

`record` logs everything, not the top few: a study that records only what the score
already likes cannot test the score. Neither a recorded score nor a recorded outcome can
be overwritten.

The report will refuse to draw a conclusion for a while, and that is the study working.
It needs resolved outcomes, outcomes need expiries to pass, and the unit of independence
is the (symbol, expiry) cluster rather than the row: one scan of one chain produces
dozens of candidates that share an underlying, a session and a surface, so they resolve
together. Below 20 clusters it says so and stops.

### Backups

```bash
venv\Scripts\python -m optscan backup
```

Copies the database through sqlite's own backup API rather than the filesystem, so a
copy taken while something is writing is not torn, and then opens the copy and runs an
integrity check on it. The parquet captures are mirrored incrementally: a daily run
costs a day, not the history.

Old database copies rotate at 30. **The capture mirror is never rotated**, because
deleting from it would delete the history it exists to protect.

Backups land in `../optscan-backups` unless `OPTSCAN_BACKUP_DIR` says otherwise. A
backup on the same drive survives a mistake but not the drive, and the command says
which one you have rather than letting the word imply the stronger claim.

### Dashboard

```bash
venv\Scripts\python -m optscan serve
```

Serves the API on http://127.0.0.1:8000, and the dashboard at the same address once the
frontend has been built. Six views: ranked candidates with the score breakdown on
expand, the full chain grid with heatmaps on implied vol and volume, the underlying's
candles and volatility surface, support and resistance levels, a payoff diagram for any
position you can build from the stored chain, and the positions you hold.

```bash
npm --prefix frontend run build     # then optscan serve hosts it at :8000
npm --prefix frontend run dev       # or Vite on :5173, proxying /api to :8000
```

Three things about the dashboard are deliberate and worth knowing before you read a
number off it:

- **It shows the last stored capture, not the market**, unless the live feed is on. The
  header carries the capture time and its age on every screen, and a capture over six
  hours old is badged stale.
- **A stalled live feed is reported as stalled.** A stream can stay open and silent,
  which no error event ever reports, so the badge is driven by how long it has actually
  been since anything arrived rather than by what the connection claims about itself.
- **Failures are on the screen, not in the console.** A panel that cannot load says why
  in the API's own words and offers a retry. If the server is unreachable that is one
  sentence naming the outage rather than the same error once per panel. A panel that
  throws while rendering is contained: it says so and the rest of the app keeps working,
  rather than leaving a blank page that looks like a market with nothing in it.

---

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

---

## Develop

```bash
venv\Scripts\python -m pytest
venv\Scripts\python -m ruff check .
venv\Scripts\python -m ruff format .
```

Coverage, with a floor of 80 percent on every individual module in `analytics/` and
`screener/`:

```bash
venv\Scripts\python -m pytest --cov=src/optscan
venv\Scripts\python scripts/coverage_floor.py
```

The floor is per module rather than per package on purpose. A package total is
dominated by its largest files: when that check was written the two packages together
reported 90 percent while the module deciding what a held position is worth sat at 18.

Nothing in the suite touches the network. `tests/fixtures/spy_chain_snapshot.json` is a
real captured chain and `tests/fixtures/spy_daily_bars.json` is 1100 real sessions. Both
are real rather than synthetic on purpose: synthetic data agrees with whatever a
detector happens to do, which is the opposite of what the counting tests are for.

---

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
- The gaps module finds candidates for review, not edge. Its thresholds are
  uncalibrated, and one of its four detectors is switched off because it had no
  working baseline.
- The payoff diagram's T+0 curve reprices every leg at the volatility it was entered
  at. A real move, especially a move down, usually comes with a rise in volatility
  that works against a short premium position, so that curve is optimistic in exactly
  the direction that matters.
- Validation settles at expiry, which is a counterfactual: most premium is not held to
  expiry. It is used because probability of profit is defined there, and because it is
  the worse tail. Every report says so.

---

## What this tool does not do

- **It does not place orders.** It ranks and displays. There is no broker write path in
  the codebase and no plan for one.
- **It does not give advice.** A high score is a candidate to review, not a
  recommendation, and nobody here is a licensed advisor.
- **It does not claim its scores predict anything.** Phase 8 built the mechanism to test
  that claim and the test has not returned an answer yet. The honest answer may be no.
- **It has never resolved a real outcome.** The settling path is covered by offline
  tests only. The first real settlement cannot happen before the first logged expiry
  passes.
- **It does not know its own IV rank yet.** Only a handful of sessions have been
  captured, so every IV rank reads `insufficient`. That is correct behaviour, not a bug,
  and only time fixes it.
- **It has never run against Tradier.** No token has ever existed for it. The adapter's
  fixtures are built from published schemas rather than captured responses.
- **It does not backfill.** Nothing reconstructs a chain from before the day you started
  running it, which is why the scheduled jobs matter more than any feature in here.
- **It is not multi-user, hosted, or secured.** It binds to 127.0.0.1, has no
  authentication, and assumes the only person who can reach it is you.
