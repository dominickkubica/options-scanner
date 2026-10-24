## Commands
```
venv\Scripts\python -m pytest         # tests
venv\Scripts\python -m ruff check .   # lint
venv\Scripts\python -m ruff format .  # format
venv\Scripts\python -m optscan status # market state, watchlist, recent captures
venv\Scripts\python -m optscan health # are the recurring jobs actually running
venv\Scripts\python -m optscan manage # mark held positions, evaluate, alert once
venv\Scripts\python -m optscan record # log every scored candidate for validation
venv\Scripts\python -m optscan signals # market conditions worth knowing, delivered once
venv\Scripts\python -m optscan signals --dry-run --all # preview without consuming suppression
venv\Scripts\python -m optscan signals --recent 14 # what was actually delivered
venv\Scripts\python -m optscan backtest --list-rules # the entry rules and their parameters
venv\Scripts\python -m optscan backtest --entry rsi_below --param threshold=30 --group tech
venv\Scripts\python -m optscan backtest --mode short_put --symbols QQQ AAPL --slippage 0.05
venv\Scripts\python -m optscan backtest --sweep threshold=20,25,30,35 --symbols AAPL MSFT
venv\Scripts\python -m optscan backtest ... --json   # machine readable, for an agent to read
venv\Scripts\python -m optscan search --group tech # grid search, holdout, multiplicity bar
venv\Scripts\python -m optscan search --group tech --json
venv\Scripts\python -m optscan ideas          # what is triggering a validated strategy
venv\Scripts\python -m optscan ideas --evidence # the full record, retired ones included
# The dashboard's Trade ideas tab is the same data: GET /api/ideas
venv\Scripts\python -m optscan validate # does the score actually separate outcomes
venv\Scripts\python -m optscan schedule # the recurring jobs and whether Windows has them
venv\Scripts\python -m optscan backup # copy the db, mirror the captures, verify
venv\Scripts\python -m optscan shortcut # Desktop launcher for the dashboard
venv\Scripts\python -m optscan import <csv> # take in a broker activity export
venv\Scripts\python -m optscan import-history <csv|dir> # vendor daily history, with its IV30
venv\Scripts\python -m optscan history # what downloaded vendor history is held
venv\Scripts\python -m optscan prices sync [GROUP...] # bulk daily bars for a universe
venv\Scripts\python -m optscan prices groups # the curated symbol groups and their age
venv\Scripts\python -m optscan snapshot --universe --provider alpaca # chains for every symbol
venv\Scripts\python -m optscan trades  # realized results from imported statements
venv\Scripts\python scripts/coverage_floor.py  # per module floor, after pytest --cov
venv\Scripts\python -m optscan serve  # API on 8000, plus the UI if it is built
npm --prefix frontend run dev         # Vite on 5173, proxying /api to 8000
npm --prefix frontend run build       # emit frontend/dist for optscan serve
npm --prefix frontend test            # compute.js against levels.py; pytest runs it too
venv\Scripts\python -m pip install -e ".[dev]" -c constraints.txt   # the tested tree
venv\Scripts\optscan premium sync      # cache QQQ/SPY 0-1 DTE option minute trades (resumable)
venv\Scripts\optscan premium study     # stop x DTE, target x DTE, side x trend, every k
venv\Scripts\optscan premium reconcile # your own 0DTE fills against the study's marks
```

Never start either server through Bash. Use the preview tool with the `optscan-api`
and `optscan-web` entries in `.claude/launch.json`.
