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

**Phases 0 to 4 are complete and committed.** Phase 5 is the live data feed.

**Phase 5 is authorized.** The user gave the go ahead on 2026-07-30 and made two
choices at the same time, so do not ask again:

- **Build against Tradier**, not Schwab. Schwab needs three legged OAuth with a browser
  redirect and manual app approval, plus refresh tokens that expire weekly, so it
  cannot be set up unattended. Tradier is a bearer token and has a streaming endpoint.
- **Build, test, and commit autonomously** to `main`, the same workflow as Phases 0
  to 4.

Do not start Phase 6 or later without asking.

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

### Next: Phase 5, live data and a real broker feed

Goal, from the roadmap: the numbers move. Exit criteria: the dashboard updates during
market hours without manual refresh, and degrades honestly when the feed drops.

Steps, in order:

1. **Check for a token before anything else**, and branch on the answer:
   ```
   venv\Scripts\python -c "from optscan.config import get_settings; print(get_settings().safe_summary()['credentials_set'])"
   ```
   That prints credential names only, never values. Keep it that way: never print, log,
   or commit a token. As of 2026-07-30 nothing is set, and `OPTSCAN_TRADIER_TOKEN=` is
   present but empty in `.env`. A free sandbox token comes from developer.tradier.com,
   and only the user can create it.
   - Token set: build the adapter and exercise it against real responses.
   - Token unset: do not stall. Build everything that does not need the vendor, listed
     below, driven by a fake provider. Write the adapter against the documented API
     with recorded fixtures, and say plainly that the live feed is unverified.
2. `providers/tradier.py`, behind the existing `MarketDataProvider` interface, plus its
   entry in `get_provider`. **Add the Tradier client to the ruff TID251 banned-api list
   in `pyproject.toml` in the same commit.** DECISIONS.md has said since Phase 0 that
   every new vendor goes on that list, and it is the easiest rule in the project to
   forget because nothing fails until someone imports the vendor somewhere else.
3. Token handling through `optscan.config` only. Refresh state, if any, stays out of
   git: `data/` and `.env` are both gitignored.
4. Transport from FastAPI to the browser, delta updates only. See the open question
   below before choosing.
5. Rate limiting, request budgeting, and a chain cache TTL. All configurable, none
   hardcoded, same as every other threshold in this project.
6. Connection state in the UI and market open / closed / pre / post handling. Phase 4
   already puts a `Provenance` on every payload carrying market data and renders it in
   every panel header. Extend that rather than inventing a parallel mechanism.

**The open question Phase 4 left, quoted from DECISIONS.md:**

> When a real time provider arrives, the choice is between pushing over websockets and
> letting the client poll a cheap endpoint. Pushing is the better experience and the
> harder thing to keep honest, because a partially updated screen where the chain is
> live and the scan is four minutes old is worse than one that is uniformly four
> minutes old and says so.

Decide it deliberately and record the reasoning. Whatever you pick, the screen must
never show a mix of ages without saying so.

**The highest consequence risk in this phase:** an IV history that silently mixes
yfinance and Tradier marks is corrupt in a way that is nearly impossible to detect
afterwards, and it poisons the one signal the whole tool is built around. Every record
already carries `source` as well as `fetched_at` for exactly this reason. Make a
provider switch visible in the stored data, and never merge marks from two vendors
into one series.

**Do not code Tradier's endpoints, auth headers, streaming session flow, or rate
limits from memory.** Fetch the current developer documentation, verify the shapes you
depend on, and record in DECISIONS.md what you verified and when. This project already
has one file whose fragility comes from matching vendor error text.

**Do not break Phase 4.** yfinance stays the default. `optscan scan`, `optscan
snapshot`, and the stored snapshot dashboard keep working as they do now. If live data
becomes available the UI must distinguish live from stored explicitly, per panel, and
never quietly relabel stored data as live. `REALTIME_PROVIDERS` in
`api/routers/health.py` is an empty frozenset today; update it only when a real time
provider actually exists.

**Live verification needs market hours.** The exit criterion cannot be checked outside
a session, so build it, test it against a fake feed and a frozen clock, and state that
live verification is outstanding rather than claiming it passed. An unverified claim
in this project is worse than an admitted gap.

### Watch out for

- **The daily snapshot job must keep running through every phase.** It is registered in
  Windows Task Scheduler as `OptscanDailySnapshot` at 12:45 machine time, which is
  15:45 New York. IV rank needs months of history, no free source sells it after the
  fact, and every day the job does not run is a permanent hole. Check `optscan status`
  before and after any change that touches providers, config, or storage.
- Only one day of snapshot history exists, so every IV rank in the UI reads
  `insufficient` with a caveat under it. That is correct behaviour, not a bug.
- API tests must stay offline. The provider is injected through `provider_factory_dep`
  and overridden with a fake; never let a test reach the real one.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated and
  still blocked on history. Phase 8.
