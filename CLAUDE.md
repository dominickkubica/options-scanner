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

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase
Phase 1 complete: provider interface, yfinance adapter, models, parquet snapshot
storage, sqlite watchlist and run manifest, daily snapshot job.

Phase 2 (analytics core) is next, and it is the hardest correctness work in the
project. Tests with hand verified expected values come before implementations, and
greeks get validated against a public BSM calculator to four decimals. Do not start
work on a later phase without asking.
