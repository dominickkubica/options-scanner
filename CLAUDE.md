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

**Phases 0 to 4 are complete and committed.** Do not start work on a later phase
without asking. Phase 5 is the live data feed: a real time provider (Schwab or
Tradier), and the websockets the static dashboard deliberately does not have.

### The dashboard, as built

`optscan serve` runs uvicorn on the API and hosts `frontend/dist` at the same address
when it has been built. In development, `npm --prefix frontend run dev` puts Vite on
5173 proxying `/api` to 8000. Both preview servers are registered in `.claude/launch.json`.

```
src/optscan/api/
  schemas.py    wire formats, separate from the domain models on purpose
  deps.py       settings, screen config, the solved symbol cache, the two fetches
  views.py      domain objects to wire formats, one place, where None survives
  app.py        the app, CORS for 5173, the built frontend mounted at the root
  routers/      health, watchlist, symbols (summary/chain/history), scan+gaps, payoff
frontend/src/
  api.js        every call, surfacing the server's own `detail` string on failure
  format.js     formatting, and the rule that null renders "n/a" and never 0
  views/        Opportunities, Chain, Underlying, Payoff
  components/   common.jsx, charts.jsx (hand rolled SVG), Candles.jsx
```

### Rules the dashboard adds

- **Stored snapshots only.** No live refresh button until Phase 5. The header shows the
  capture time and age everywhere, and over six hours is badged stale.
- **Two request time fetches, and both state their failures:** the corporate calendar
  (or the screen silently skips its earnings exclusion) and daily candles (nothing
  stores them). Anything unavailable returns an empty result carrying the reason.
- **Never send a zero you do not mean.** A contract with no solvable vol serializes
  `iv: null` plus the solver's named refusal, and every listed strike keeps its row.
  The frontend mirrors this: `n/a`, never a `|| 0` fallback.
- **The payoff request carries no prices.** The browser names strikes and directions;
  the server prices them from the same solved snapshot as the rest of the page.
- **Thresholds stay in config.** The browser asks for a chain with no expiry and the
  server picks the first one at or beyond `min_dte`, so the UI never hardcodes a
  screen threshold.
- **Charts need bands for the same reason the analytics do.** A 0.00 by 0.05 wing
  strike solves to a real 195 percent vol and flattens a 14 vol smile to nothing. Skew
  plots within 20 percent of spot, the chain grid defaults to 25 percent, and both say
  how many strikes are hidden.
- **The solved symbol cache is locked per key.** Keyed on `fetched_at` so a new capture
  invalidates it, and guarded by a per key lock because the three panels that ask at
  once would otherwise all miss and all solve. See `test_concurrent_requests_for_one_symbol_solve_it_once`.

### Watch out for

- Only one day of snapshot history exists, so every IV rank in the UI reads
  `insufficient` with a caveat under it. That is correct behaviour, not a bug.
- API tests must stay offline. The provider is injected through `provider_factory_dep`
  and overridden with a fake; never let a test reach the real one.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated and
  still blocked on history. Phase 8.
