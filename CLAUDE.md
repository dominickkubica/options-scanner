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
  analytics/        greeks.py, iv.py, probability.py, levels.py
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

**Phases 0 to 5 are complete and committed.** Phase 6 is levels and projections, and it
is **not authorized**. Ask before starting it, and before anything later.

### What Phase 5 built

A Tradier adapter, a polling live feed, and an SSE stream to the browser.

```
src/optscan/providers/
  tradier.py        REST adapter: quotes, expirations, chains, daily bars
  ratelimit.py      token bucket sized from the documented per minute limit
src/optscan/live/
  hub.py            the refresh loop, the delta encoder, the subscriber fan-out
src/optscan/api/routers/live.py    GET /api/live/status and /api/live/stream
frontend/src/live.js               EventSource client and the delta application rule
```

The transport decision and its reasoning are in DECISIONS.md under 2026-07-31. The
short version: Tradier's free sandbox has no streaming endpoint at all and is fifteen
minutes delayed, so there is no vendor push to forward. The server polls, and SSE tells
the browser a cycle landed.

### Rules Phase 5 adds

- **The unit of update is a whole cycle, never a field.** One quote, one chain, one
  solve, one `fetched_at` across all of it. That is what makes a delta safe: a contract
  that did not move holds the same value at this version as at the last, so the table is
  entirely as of the newest version rather than a mixture of ages.
- **A fetch that fails or comes back partial never becomes a version.** It emits a
  status event. The previous cycle stays on screen and its age climbs, which is the
  honest picture.
- **A contract that leaves the chain is named in `removed`.** A row nobody mentions
  again is one the browser renders forever at its last price.
- **The grid is live or stored, never a blend.** Not an overlay. Different strike sets,
  different ages; the table renders one source and its header says which.
- **`realtime` is a function of the settings, not a set of provider names.** Sandbox is
  delayed regardless; production depends on an entitlement no response announces, so
  `tradier_realtime_entitled` is asked and defaults to false. `delay_minutes` null means
  unknown, not zero.
- **Nothing is polled while the market is closed.** Polling slows by a configurable
  multiple outside the regular session. Session state comes from the offline market
  calendar, not from a vendor clock endpoint.
- **The live feed never writes to storage.** The daily snapshot job stays the single
  writer of the IV history.
- **The hub and the chain endpoint must resolve an unnamed expiry identically.** They
  did not at first, and the symptom was a stream that connected, cycles that arrived, a
  header that said live, and a grid that never moved. `LiveHub._default_expiry` and
  `symbols.py` both use `min_dte` from screen.yaml, and a test pins it.

### What is verified, and what is not

Verified in a browser on 2026-07-31 with the market open, against **yfinance**: the
stream connects, cycles arrive on the interval with no refresh, the numbers move, the
header shows connection and session state, and stopping the API leaves the last cycle
on screen with its age climbing.

**Nothing has been run against Tradier.** No token existed. `tests/fixtures/tradier/`
is built from the published response schemas, not captured, and its README says so.

**The single thing only the user can do:** create a free sandbox token at
developer.tradier.com and paste it into the empty `OPTSCAN_TRADIER_TOKEN=` line in
`.env`. Never enter it on their behalf, never print or commit a token value.
`safe_summary()` reports credential names only.

**When a token appears, in this order:**

1. `venv\Scripts\python -c "from optscan.config import get_settings; print(get_settings().safe_summary()['credentials_set'])"`
2. Recapture `tests/fixtures/tradier/*.json` from the sandbox and delete the paragraph
   in its README that says they are constructed from documentation.
3. Check the shapes the adapter guesses at because the docs do not state them: whether
   `quotes.unmatched_symbols` really appears for an unknown ticker, and whether the
   epoch fields are milliseconds. `_clean_epoch` infers the unit by magnitude.
4. Only then consider `OPTSCAN_PROVIDER=tradier`. Switching weakens the earnings
   exclusion, visibly, because Tradier sells no corporate calendar, and it starts a new
   IV history: the yfinance sessions are excluded by design and the UI says so.

### Watch out for

- **The daily snapshot job must keep running through every phase.** Registered in
  Windows Task Scheduler as `OptscanDailySnapshot` at 12:45 machine time, which is 15:45
  New York. IV rank needs months of history, no free source sells it after the fact, and
  every day the job does not run is a permanent hole. Check `optscan status` before and
  after any change touching providers, config, or storage.
- **`OPTSCAN_LIVE_ENABLED` is false by default and should stay that way** until somebody
  asks for live data. With it false this is exactly the stored snapshot dashboard Phases
  0 to 4 built, and nothing polls anything.
- Only two days of snapshot history exist, so every IV rank in the UI reads
  `insufficient` with a caveat under it. That is correct behaviour, not a bug.
- API tests must stay offline. The provider is injected through `provider_factory_dep`
  and the hub through `live_hub`, both overridden with fakes.
- **The SSE endpoint cannot be tested through TestClient.** Starlette buffers the whole
  response and returns only on the app's final body message, which a stream never sends,
  so the request never returns. Drive `_events` directly, as `tests/test_api_live.py`
  does.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated and
  still blocked on history. Phase 8.
