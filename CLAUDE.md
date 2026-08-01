# Project: Options Selling Scanner

## Stack
Python 3.12, FastAPI, DuckDB + SQLite, pydantic v2, py_vollib, httpx, pytest, ruff.
Frontend: React + Vite + lightweight-charts. Windows dev environment.

The venv is pinned to 3.12 and lives at `venv/`. Use `venv\Scripts\python` for everything.
The system default `python` on this machine is 3.13 and is not what this project runs on.

## Rules
- Analytics functions are pure: no I/O, no network, no globals. All I/O lives in providers/ and storage/.
- Every analytics function ships with a unit test containing a hand-verified expected value.
- No hardcoded thresholds. Anything a user might tune goes in config.
- All market data flows through the MarketDataProvider interface. Never import yfinance,
  httpx, or a broker SDK outside providers/. Ruff enforces this (TID251), and every new
  vendor goes on that list in the same commit that adds the adapter.
- An IV history is per vendor. Never pool two sources into one series, and say what was
  excluded when you filter.
- Every data record carries fetched_at. Never display a number without knowing its age.
- Prefer explicit failure over silent fallback. A stale quote must be visibly stale.
- No em dashes in comments, docstrings, or UI copy.
- Secrets come from .env via optscan.config only. Nothing else reads os.environ.

## Layout
```
src/optscan/
  config.py         settings, the only reader of environment variables
  logging.py        structlog setup
  cli.py            command line entry point
  providers/        market data adapters, one per vendor
  models/           pydantic: Quote, Chain, Contract, Snapshot, Opportunity
  storage/          sqlite + duckdb writers, migrations
  analytics/        greeks.py, iv.py, probability.py, levels.py, projection.py
  screener/         rules/, scoring.py, strategies/
  jobs/             snapshot.py, scheduler.py
  live/             the polling refresh loop and its delta encoder
  api/              schemas, deps, views, app, routers/
frontend/           React app: src/views, src/components, gitignored node_modules and dist
tests/              mirrors src layout; fixtures/ holds frozen chains
data/               gitignored: snapshots, sqlite db
```

## Commands
```
venv\Scripts\python -m pytest         # tests
venv\Scripts\python -m ruff check .   # lint
venv\Scripts\python -m ruff format .  # format
venv\Scripts\python -m optscan status # market state, watchlist, recent captures
venv\Scripts\python -m optscan serve  # API on 8000, plus the UI if it is built
npm --prefix frontend run dev         # Vite on 5173, proxying /api to 8000
npm --prefix frontend run build       # emit frontend/dist for optscan serve
```

Never start either server through Bash. Use the preview tool with the `optscan-api`
and `optscan-web` entries in `.claude/launch.json`.

## Conventions worth knowing before editing
- None means unknown, 0.0 means the vendor said zero. Never collapse the two.
- Timestamps are timezone aware UTC everywhere. Naive datetimes are rejected, not guessed.
- The session a capture belongs to comes from the market calendar, never from the
  machine's date.
- Tests never touch the network. `tests/fixtures/spy_chain_snapshot.json` is a real
  captured chain, `tests/fixtures/spy_daily_bars.json` is 1100 real sessions, and
  `FakeProvider` in conftest drives the job offline. Both fixtures are real captures on
  purpose: synthetic bars agree with whatever a detector happens to do, which is the
  opposite of what the counting tests are for.
- Greek units are trader units: theta per calendar day, vega per volatility point, rho
  per rate point. See the greeks.py docstring before touching any of it.
- Analytics take thresholds as arguments with documented defaults. They never read
  config. The caller passes config values in.
- A number that cannot be computed honestly is not computed. The vol solver, IV rank,
  and the liquidity score all return a stated reason instead of a plausible value.
- Screener thresholds live in screen.yaml only. Nothing in screener/ hardcodes a
  number a user might want to change.
- Every filter rejection carries a reason, and the tally is printed. An unexplained
  empty result table is how a screener loses its user.
- Before believing any detector, check whether it is measuring a constant offset. That
  mistake has now been made eight times, most recently twice in one sitting in
  `analytics/levels.py`: a swing prominence filter thresholding a quantity that is one
  ATR by construction, and a touch count that was really measuring pivot density. Run
  it against tests/fixtures and count the hits before believing any of it. If the
  measure needs a baseline, the baseline usually has to account for structure rather
  than being uniform: see `occupancy_share`, where the null is how much time price
  actually spent at each price.
- Two filters aimed at the same thing will hide each other. The confounded one in front
  of the principled one does not merely fail to help, it starves the good one of the
  data it needs.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase

**Phases 0 to 6 are complete and committed.** Phase 7 is positions and risk, and it is
**not authorized**. Ask before starting it, and before anything later.

### What Phase 6 built

```
src/optscan/analytics/
  levels.py       backward looking: pivots, clustering, the significance test,
                  volume profile, round numbers, ATR, Bollinger, realized vol
  projection.py   forward looking: the expected move cone, terminal distributions
src/optscan/api/routers/levels.py    GET /api/symbols/{symbol}/levels
frontend/src/components/LevelsChart.jsx   the one chart
frontend/src/views/Levels.jsx             the panel around it
tests/fixtures/spy_daily_bars.json        1100 real SPY sessions, 2022 to 2026
```

The exit criterion is met: one chart carries price, the levels it respected, the
expected move cone projected to each expiry, and the screener's candidate strikes shaded
by probability of profit.

### The thing to understand before touching levels.py

**Almost all of that module is a baseline, not a detector.** Clustering swing pivots and
keeping the ones touched twice publishes 31 levels on four years of SPY, and every one
of them looks like evidence. They are not: 123 pivots scattered across a 390 point range
put about 1.45 in each 4.5 point band by arithmetic alone. So each cluster is compared
against how many touches its own band would have collected by chance *given how much
time price actually spent there*, and only the excess survives. That takes 31 down to 1.

The null had to account for occupancy rather than being uniform. Price spends months in
a congestion zone and crosses a gap in a day, so a uniform null would call everything
inside the zone significant and everything outside it noise, which is the same trap in a
new hat. `occupancy_share` is that null.

`min_prominence_atr` defaults to 0.0 and is documented as a trap rather than deleted. It
thresholds a quantity that is one ATR by construction, so it selects almost nothing, and
turning it up far enough to bite starves the test that works.

### Rules Phase 6 adds

- **Zero levels is a finding, not an empty panel.** The UI says how many candidates were
  tested and why none survived. An unexplained blank reads as a broken job.
- **Strength is only comparable within a kind.** A swing level's restates a p-value, a
  volume node's is a share of the busiest bin, a round number's is a constant standing
  in for the fact that nothing was measured. Never sort them together.
- **A round number's `p_value` is null, not 1.0.** Null means not tested; 1.0 would mean
  tested and failed.
- **The cone uses each expiry's own implied vol.** One vol over a sqrt(t) curve is wrong
  for anyone selling more than one expiry, and most wrong around events. Bands are
  lognormal, not symmetric.
- **The variance risk premium is refused across mismatched tenors.** Comparing 30 day
  realized against 4 day implied is the term structure talking. Nothing within a factor
  of two of the realized window means no number is published.
- **The levels payload is one request carrying two provenances.** It is the only screen
  that draws a just fetched number and a stored one on the same axes, and splitting it
  would let the browser assemble one picture from four moments.
- **A candidate strike's POP belongs to its position, not to the strike.** Neighbouring
  strikes can read 62 and 75 percent when one is a spread and one a single leg. The
  panel says so, because it looks like a bug otherwise.

### Watch out for

- **The daily snapshot job must keep running through every phase.** Registered in
  Windows Task Scheduler as `OptscanDailySnapshot` at 12:45 machine time, 15:45 New
  York. IV rank needs months of history, no free source sells it after the fact, and
  every day the job does not run is a permanent hole. Check `optscan status` before and
  after any change touching providers, config, or storage.
- Two captured sessions exist as of 2026-07-31, so every IV rank still reads
  `insufficient`. Correct behaviour, not a bug.
- **`OPTSCAN_LIVE_ENABLED` is false by default** and should stay that way until somebody
  asks for live data. See the Phase 5 notes in DECISIONS.md.
- **The Tradier adapter has still never run against Tradier.** No token exists.
  `tests/fixtures/tradier/` is built from published schemas, not captured. The ordered
  checklist for when a token appears is in the Phase 5 DECISIONS entry.
- API tests must stay offline. The provider is injected through `provider_factory_dep`
  and the hub through `live_hub`, both overridden with fakes.
- **The SSE endpoint cannot be tested through TestClient.** Starlette buffers the whole
  response. Drive `_events` directly, as `tests/test_api_live.py` does.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated and
  still blocked on history. Phase 8.
- **A historical options backfill is the highest leverage thing available.** IV rank
  needs 20 observations minimum and 180 for high confidence, and at one capture a day
  that is nine months away. ThetaData's docs advertise a free tier with one year of EOD
  history, though their pricing page shows no zero tier, so it needs an account to
  confirm. Cboe DataShop's EOD file snapshots at 15:45, which is exactly this project's
  capture time. Buy raw chains and re-solve them with our own solver; never adopt a
  vendor's IV rank, which would import their rate and dividend assumptions.
