# Project: Options Selling Scanner

## Stack
Python 3.12, FastAPI, DuckDB + SQLite, pydantic v2, py_vollib, pytest, ruff.
Frontend: React + Vite + lightweight-charts. Windows dev environment.

The venv is pinned to 3.12 and lives at `venv/`. Use `venv\Scripts\python` for everything.
The system default `python` on this machine is 3.13 and is not what this project runs on.

## Rules
- Analytics functions are pure: no I/O, no network, no globals. All I/O lives in providers/ and storage/.
- Every analytics function ships with a unit test containing a hand-verified expected value.
- No hardcoded thresholds. Anything a user might tune goes in config.
- All market data flows through the MarketDataProvider interface. Never import yfinance
  or a broker SDK outside providers/. Ruff enforces this (TID251).
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
  analytics/        greeks.py, iv.py, probability.py, levels.py
  screener/         rules/, scoring.py, strategies/
  jobs/             snapshot.py, scheduler.py
  api/              FastAPI routers
frontend/           React app (Phase 4)
tests/              mirrors src layout; fixtures/ holds frozen chains
data/               gitignored: snapshots, sqlite db
```

## Commands
```
venv\Scripts\python -m pytest         # tests
venv\Scripts\python -m ruff check .   # lint
venv\Scripts\python -m ruff format .  # format
venv\Scripts\python -m optscan status # market state, watchlist, recent captures
```

## Conventions worth knowing before editing
- None means unknown, 0.0 means the vendor said zero. Never collapse the two.
- Timestamps are timezone aware UTC everywhere. Naive datetimes are rejected, not guessed.
- The session a capture belongs to comes from the market calendar, never from the
  machine's date.
- Tests never touch the network. `tests/fixtures/spy_chain_snapshot.json` is a real
  captured chain, and `FakeProvider` in conftest drives the job offline.
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
- Before believing any detector that fires on a lot of contracts, check whether it is
  measuring a constant offset. That mistake has now been made three times: the
  expected move multiplier, the vertical gap detector, and the parity carry
  assumption. Run it against tests/fixtures and count the hits.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase

**Phase 4 (dashboard v1, static) is IN PROGRESS.** Phases 0 to 3 are complete and
committed. Do not start work on a later phase without asking.

### Phase 4 goal and exit criteria

FastAPI serves JSON, React consumes it, no websockets (those are Phase 5). Exit
criteria from the roadmap: pick a ticker in the UI and see chain, charts, and
candidates without touching a terminal. Four views:

- Opportunities: sortable/filterable ranked table, score breakdown on expand
- Chain: full grid, heatmap on IV and volume, greeks columns
- Underlying detail: candles + volume, IV rank gauge, term structure curve, skew curve
- Payoff: P/L at expiry and at T+0, breakevens, max profit and loss

### Done so far

- `analytics/payoff.py` plus 28 tests, all passing. Expiry and T+0 curves, breakevens
  solved exactly by interpolation on the piecewise linear segments rather than
  sampled, and bounded/unbounded extremes. Note the asymmetry it encodes: only the
  upside can be unbounded, because a stock cannot fall below zero, so a short put has
  a real maximum loss and a short call does not.
- `api/schemas.py`, the wire formats. Deliberately separate from the domain models so
  a UI change never pulls on what the analytics depend on. Every payload carrying
  market data also carries a `Provenance` with its age and a stale flag.
- `pyproject.toml` now depends on fastapi and uvicorn, with httpx as a dev dependency
  for testing the API without a running server. Already installed in the venv.

### Next steps, in order

1. `api/deps.py`: settings and screen config access, plus a solved-symbol cache keyed
   on the snapshot's `fetched_at` so a new capture invalidates it automatically.
   Solving a chain is several hundred milliseconds and three panels ask for the same
   symbol at once. Also a `frontend_dist()` helper returning the built frontend path
   or None in development.
2. `api/routers/`: health, watchlist, symbol summary, chain (per expiry), history
   (candles), scan/opportunities, gaps, payoff. Payoff must be computed server side
   because the T+0 curve needs BSM repricing.
3. `api/app.py`: the FastAPI app, CORS for the Vite dev server on 5173, and mounting
   the built frontend when it exists.
4. Tests via `fastapi.testclient.TestClient` against the frozen fixture. The API must
   be testable with no network and no running server.
5. `frontend/`: Vite + React. Node 20.20.2 and npm 10.8.2 are installed. Write
   `package.json`, `vite.config.js`, `index.html` and `src/` directly rather than
   running `npm create vite`, which is interactive. lightweight-charts for candles,
   hand rolled SVG or Recharts for payoff, term structure, and skew.
6. Add a `.claude/launch.json` entry so the dev server starts through the preview
   tool. Never run a dev server through Bash.
7. Verify in the browser preview, screenshot it, then update README, DECISIONS, and
   this file, and commit.

### Watch out for

- The API must never invent a number. A missing greek serializes as null, never zero:
  a chart that draws zero for "unknown" is lying invisibly.
- `optscan scan` currently takes seconds across the watchlist. The dashboard will need
  the cache in step 1 or every page load will re-solve everything.
- Only one day of snapshot history exists, so every IV rank in the UI will be
  `insufficient` with a caveat. That is correct behaviour and the UI has to show it
  rather than render an empty gauge.
