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
  models/           pydantic: Quote, Chain, Contract, Snapshot, Opportunity, Position
  storage/          sqlite + duckdb writers, migrations
  analytics/        greeks.py, iv.py, probability.py, levels.py, projection.py,
                    portfolio.py, triggers.py
  screener/         rules/, scoring.py, strategies/
  jobs/             snapshot.py, scheduler.py, manage.py
  live/             the polling refresh loop and its delta encoder
  alerts.py         alert sinks, and once-per-condition delivery
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
venv\Scripts\python -m optscan manage # mark held positions, evaluate, alert once
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

**Phases 0 to 7 are complete and committed.** Phase 8 is validation, and it is **not
authorized**. Ask before starting it, and before anything later.

Phase 8 is the one the roadmap says not to skip: it logs every scored opportunity,
resolves outcomes at expiry, and reports whether high scores actually outperform low
ones. Everything this tool currently asserts is unvalidated, and several modules say so
in their own docstrings.

### What Phase 7 built

```
src/optscan/
  models/position.py          Position and PositionLeg, entered by a person
  storage/positions.py        CRUD plus the alert suppression table
  analytics/portfolio.py      per position and aggregate greeks, beta weighting
  analytics/triggers.py       profit target, DTE, delta breach, tested, events
  screener/positions.py       marking a held position against a solved chain
  jobs/manage.py              the management run
  alerts.py                   sinks, and once-per-condition delivery
  api/routers/positions.py    GET /api/positions, read only
frontend/src/views/Positions.jsx
```

```
optscan position add SPY --expiry 2026-09-18 --leg sell:P:700:5.20 --leg buy:P:690:2.10
optscan position list
optscan position close 1 --value -86 --commission 1.30
optscan manage            # mark, evaluate, deliver new alerts
optscan manage --offline  # no provider: no beta, no event triggers, says so
```

### Rules Phase 7 adds

- **The fill price is required and has no default.** It is the only number in this
  project that cannot be recomputed, and P/L is measured against it rather than against
  a mark. The CLI refuses a leg spec without it.
- **Held positions mark at what it costs to close**, not at the mid: a short leg at the
  ask, a long leg at the bid. The mid overstates a short book by half the spread on
  every leg, worst in the range where a profit target fires.
- **One sign convention.** Credit positive throughout, so profit is always
  `signed_fill - signed_value` whichever direction the leg is.
- **Greeks refuse on a missing leg; profit sums over what marked.** A partial delta gets
  used to size a hedge. A partial profit is still the profit of the positions in it.
- **Beta is None when it cannot be estimated, never 1.0.** Below sixty overlapping
  sessions the portfolio withholds its beta weighted delta and says why.
- **Early assignment is decided by extrinsic against the dividend**, not by moneyness.
- **Delta breach is per leg.** A condor's net can read flat while one side is tested.
- **There is no stop loss trigger, deliberately.** See the docstring in triggers.py: on
  a short option it closes exactly the positions that were about to recover, and
  shipping it would need Phase 8 evidence.
- **Alerts fire once per condition, suppressed in the database**, and a delivery that
  no sink accepted is not recorded, so it retries rather than being lost.
- **Position entry is CLI only and the API is read only.** A GET that delivered alerts
  would fire them on every browser refresh.

### Watch out for

- **The daily snapshot job must keep running through every phase.** Windows Task
  Scheduler, `OptscanDailySnapshot`, 12:45 machine time and 15:45 New York. IV rank
  needs months of history, no free source sells it after the fact, and every day the job
  does not run is a permanent hole. Check `optscan status` before and after any change
  touching providers, config, or storage.
- **`optscan manage` is not scheduled.** It is safe to run repeatedly and would suit a
  task every fifteen minutes during market hours, but nothing registers it yet. Adding
  that is a reasonable small piece of work and was not part of Phase 7.
- **`WebhookSink` has never been run against a real endpoint.** No webhook exists to
  test against. The payload is deliberately plain rather than shaped for one vendor, so
  Discord will need its `content` key.
- Two captured sessions exist, so every IV rank still reads `insufficient`.
- **`OPTSCAN_LIVE_ENABLED` is false by default** and should stay that way until asked.
- **The Tradier adapter has still never run against Tradier.** No token exists, and
  `tests/fixtures/tradier/` is built from published schemas rather than captured. The
  ordered checklist is in the Phase 5 DECISIONS entry.
- **The SSE endpoint cannot be tested through TestClient.** Starlette buffers the whole
  response. Drive `_events` directly, as `tests/test_api_live.py` does.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated and
  still blocked on history. Phase 8.
- **A historical options backfill is still the highest leverage thing available.** IV
  rank needs 180 observations for high confidence and at one capture a day that is nine
  months out. ThetaData's docs advertise a free year of EOD history while their pricing
  page shows no zero tier, so it needs an account to settle; their terminal is a local
  Java jar serving REST on 127.0.0.1:25503. Cboe DataShop's EOD file snapshots at 15:45,
  exactly this project's own capture time. Buy raw chains and re-solve with our own
  solver; never adopt a vendor's IV rank.
