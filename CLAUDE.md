# Project: Options Selling Scanner

**Wiring facts come from the generated map.** A hook adds the map's rows and notes for whatever your prompt names; for anything else, Grep `map/generated/` and `map/notes.md` for a name instead of reading whole files (`MAP.md` is the overview). After a wiring change run `optscan map generate`; if `optscan map check` reports a stale note, re-read it, fix it, then `optscan map stamp <key>`. `tests/test_map.py` and the pre-commit `map` hook enforce it.

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
See `map/generated/modules.md` for every module and what it does, and `MAP.md` for how
they connect. Short version: `providers/` reach vendors, `storage/` reaches disk,
`analytics/` and `screener/` are pure, `jobs/` and `api/deps.py` orchestrate,
`frontend/` is React over `/api`.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Working efficiently

Everything put into context is re-read on every later turn, so:
- Edit with small targeted edits; never rewrite a whole file to change part of it.
- Locate with Grep (or the map), then read only the line range needed.
- Run the relevant test file with `-q --tb=short`, not the whole suite; the pre-push hook
  and CI run everything.
- Check the dashboard with page text before screenshots.
- Write code already formatted (ruff, 100 columns), so the formatter changes nothing.

## Read when relevant

- `docs/agent/commands.md`: Commands, every CLI command and how to run checks
- `docs/agent/conventions-worth-knowing-before-editing.md`: Conventions worth knowing before editing, read before changing code in an area
- `docs/agent/current-phase.md`: Current phase, what is built, what is validated, where the research stands
