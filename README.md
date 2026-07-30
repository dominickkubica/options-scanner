# Options Selling Scanner

A local-first tool that ingests options chains, computes its own greeks and volatility
analytics, scores premium-selling candidates, and renders a dashboard with charts,
levels, and projections.

Currently at **Phase 0**: the scaffold boots, is linted, and is tested. There is no
market data yet. See `DECISIONS.md` for why things are the way they are and `CLAUDE.md`
for the rules any contributor (human or agent) works under.

## Setup

```
py -3.12 -m venv venv
venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
venv\Scripts\pre-commit install
```

## Run

```
venv\Scripts\python -m optscan          # boot, create data dirs, log config
venv\Scripts\python -m optscan --check  # print resolved config, touch nothing
venv\Scripts\optscan --version
```

## Develop

```
venv\Scripts\python -m pytest
venv\Scripts\python -m ruff check .
venv\Scripts\python -m ruff format .
```

## What this tool does not do

- It does not place orders. It ranks and displays.
- It does not give advice. A high score is a candidate to review, not a recommendation.
- It does not claim its scores predict anything. Phase 8 exists to test that claim, and
  the honest answer may turn out to be no.
