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
                    portfolio.py, triggers.py, outcomes.py, calibration.py
  screener/         rules/, scoring.py, strategies/
  console.py        terminal colour, and the rules about when not to use it
  jobs/             snapshot.py, schedule.py, manage.py, validate.py, backup.py,
                    launcher.py, health.py
  live/             the polling refresh loop and its delta encoder
  alerts.py         alert sinks, and once-per-condition delivery
  api/              schemas, deps, views, app, routers/
frontend/           React app: src/views, src/components, gitignored node_modules and dist
scripts/            coverage_floor.py, the per module coverage gate
tests/              mirrors src layout; fixtures/ holds frozen chains
data/               gitignored: snapshots, sqlite db
```

## Commands
```
venv\Scripts\python -m pytest         # tests
venv\Scripts\python -m ruff check .   # lint
venv\Scripts\python -m ruff format .  # format
venv\Scripts\python -m optscan status # market state, watchlist, recent captures
venv\Scripts\python -m optscan health # are the recurring jobs actually running
venv\Scripts\python -m optscan manage # mark held positions, evaluate, alert once
venv\Scripts\python -m optscan record # log every scored candidate for validation
venv\Scripts\python -m optscan validate # does the score actually separate outcomes
venv\Scripts\python -m optscan schedule # the recurring jobs and whether Windows has them
venv\Scripts\python -m optscan backup # copy the db, mirror the captures, verify
venv\Scripts\python -m optscan shortcut # Desktop launcher for the dashboard
venv\Scripts\python scripts/coverage_floor.py  # per module floor, after pytest --cov
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
  mistake has now been made nine times. Run it against tests/fixtures and count the hits
  before believing any of it. If the measure needs a baseline, the baseline usually has
  to account for structure rather than being uniform: see `occupancy_share`, where the
  null is how much time price actually spent at each price.
- The ninth instance was not a measure but a **sample**: rows in the validation log share
  a chain and resolve together, so 903 of them are a few dozen observations. Whenever
  something is counted, ask what the unit of independence actually is before putting an
  interval on it. See `cluster_key` in analytics/calibration.py.
- Two filters aimed at the same thing will hide each other. The confounded one in front
  of the principled one does not merely fail to help, it starves the good one of the
  data it needs.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase

**Phases 0 to 9 are complete and committed.** The roadmap ends at 9. Anything further is
new scope and is **not authorized**: ask first.

### The state that matters most right now

**The validation study is running and it is empty.** 903 real candidates were logged on
2026-08-01 and the earliest of them expires 2026-08-21, so `optscan validate` correctly
reports nothing until then. That is the study working, not a bug.

**Four jobs are now registered with Windows** and `optscan schedule` prints them and
their live state: `snapshot` 15:45, `record` 16:15, `backup` 16:30, `resolve` 08:00, all
market time. `manage` is deliberately not installed; see the Phase 9 DECISIONS entry.

**Recording has to keep running, for the same reason the snapshot job does.** A
candidate that was never logged when it was scored cannot be settled later, and there is
no way to reconstruct what the screen would have surfaced last Tuesday. Every day
`optscan record` does not run is a permanent hole.

**The backup default is on the same drive as the data.** `optscan backup` says so every
run. Pointing `OPTSCAN_BACKUP_DIR` at another disk or a synced folder is a one line
change and the only thing that makes it a real backup.

### What Phase 8 built

```
src/optscan/
  analytics/outcomes.py       settling a short position at expiry, arithmetic only
  analytics/calibration.py    buckets, Wilson intervals, reliability, Brier
  storage/validation.py       the append only log and the outcome table
  jobs/validate.py            record, resolve, report
```

```
optscan record      # score the watchlist and log every candidate
optscan resolve     # settle logged candidates whose expiry has passed
optscan validate    # the report, or a refusal to give one
```

### The thing to understand before touching calibration.py

**Rows are not observations.** One scan of one chain produces dozens of candidates that
share an underlying, a session and a surface, so they resolve together. The unit of
independence is the **(symbol, expiry) cluster**, and every interval in the report is
widened to the cluster count rather than the row count. On the first real recording run
that is 903 rows over a few dozen clusters, and an interval on the row count would be
about five times too narrow.

Below `MIN_CLUSTERS_FOR_A_CLAIM` (20) the report draws no conclusion at all and says so.

**Separation is judged on halves, not on extreme buckets.** Comparing the top quartile
against the bottom one throws away the middle half and gives both intervals a quarter of
the sample. It was wrong on a demonstration sample where the score genuinely worked, and
a regression test now pins that shape.

### Rules Phase 8 adds

- **Log everything the screen surfaces, not the top N.** `--limit` warns when used.
- **Never overwrite a recorded score or a recorded outcome.** Both tables refuse. A
  resolved outcome stays attached to the score the candidate was actually given.
- **Calibration is measured on the event, not on profit.** A position can be breached and
  still make money; scoring on profit lets a model be wrong and look right.
- **Hold to expiry is a counterfactual and is labelled on every report.** It is used
  because probability of profit is defined at expiry, and because it is the worse tail.
- **A win rate above 60 percent with negative mean profit gets a sentence saying the
  losers are bigger than the winners.** Short premium is designed to win often.
- **Settle only against a real close.** A candidate whose expiry close cannot be found
  stays unresolved rather than being settled at an approximation, because the missing one
  is visible and the wrong one is not. Phase 9 found this was not actually true: the
  previous session fallback fired whenever the close was merely *missing*, not only when
  the market was shut, so an intraday run on expiry day settled everything against
  yesterday and labelled it a holiday. **A trading day with no bar yet is missing, not
  shut.** The gate is `is_trading_day`, and there is a regression test.

### Watch out for

- **The daily snapshot job must keep running through every phase.** Windows Task
  Scheduler, `OptscanDailySnapshot`, 12:45 machine time and 15:45 New York. Check
  `optscan status` before and after any change touching providers, config, or storage.
- **Never register a task with `schtasks /Create`.** It cannot set
  `StartWhenAvailable` or clear the battery restrictions, and its defaults on both mean
  the job silently does not run. That was live on the snapshot task until Phase 9.
  Registration goes through `Register-ScheduledTask`, and `jobs/schedule.py` is the only
  place that does it.
- **A green exit code is not evidence that work happened.** `optscan health` reads the
  `job_run` log *and* Windows, because a job that runs, finds nothing to do and exits 0
  is a tick in Task Scheduler and a hole in the history. Where the two disagree, the
  disagreement is the finding.
- **A monitor that cries wolf is worse than none.** `health` walks the market calendar
  so a weekend is not a missed session, and holds back every "it has not run" claim for
  a scheduled time earlier than `meta.job_log_started_at`, because before the log
  existed a missing row is missing evidence rather than a missed run.
- **Colour is never the only carrier.** Every coloured state also has a word, and
  anything laid out in columns pads with `console.pad`, because f-string padding counts
  the escape sequence and silently misaligns the table.
- **No outcome has ever actually been resolved.** The settling path is covered by an
  offline test with a fake provider; the first real `optscan resolve` run is outstanding
  and cannot happen before 2026-08-21.
- **`WebhookSink` has never been run against a real endpoint.**
- Two captured sessions exist, so every IV rank still reads `insufficient`.
- **`OPTSCAN_LIVE_ENABLED` is false by default** and should stay that way until asked.
- **The Tradier adapter has still never run against Tradier.** No token exists, and
  `tests/fixtures/tradier/` is built from published schemas. Checklist in the Phase 5
  DECISIONS entry.
- **The SSE endpoint cannot be tested through TestClient.** Starlette buffers the whole
  response. Drive `_events` directly, as `tests/test_api_live.py` does.
- The gaps thresholds and the vertical mispricing baseline are still uncalibrated. They
  were blocked on history, and Phase 8 is now the mechanism that will eventually
  calibrate them, but it needs resolved outcomes first.
- **A historical options backfill is still the highest leverage thing available**, and
  Phase 8 sharpens why: it would let history be scored retroactively, which is the only
  way to get resolved outcomes faster than one expiry cycle at a time. Settling does not
  need it; scoring does. ThetaData's docs advertise a free year of EOD history while
  their pricing page shows no zero tier, so it needs an account to settle; their terminal
  is a local Java jar serving REST on 127.0.0.1:25503. Cboe DataShop's EOD file snapshots
  at 15:45, exactly this project's own capture time. Buy raw chains and re-solve with our
  own solver; never adopt a vendor's IV rank.
