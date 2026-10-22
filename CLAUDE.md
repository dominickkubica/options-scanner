# Project: Options Selling Scanner

**Read `MAP.md` before tracing anything.** It is a short index into the wiring map: the
facts (modules, tables with their writers and readers, routes, caches, views, jobs,
commands) are generated into `map/generated/`, and what they cannot say (why a table
exists, the traps) is in `map/notes.md`. Open only the file the question needs. **After
any wiring change, run `optscan map generate` and commit the result with it**; if
`optscan map check` reports a note as stale, re-read it, fix it, then `optscan map stamp
<key>`. `tests/test_map.py` and the pre-commit `map` hook enforce all of it.

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
  mistake has now been made eleven times. Run it against tests/fixtures and count the hits
  before believing any of it. If the measure needs a baseline, the baseline usually has
  to account for structure rather than being uniform: see `occupancy_share`, where the
  null is how much time price actually spent at each price.
- The tenth and eleventh both arrived with the Market Chameleon import and neither was
  a detector. The tenth was a **tenor mismatch**: ranking a front expiry's vol against a
  thirty day history compares two quantities that differ structurally, not two levels.
  The eleventh was about **which components exist**: renormalizing a missing score
  component across symbols scores "no data" as though it were "excellent", so importing
  a real but low IV rank made a symbol rank worse. Both are the same shape as the rest.
  A structural difference read as a quality difference, and both were invisible until
  real data with uneven coverage arrived. **Whenever coverage is uneven across the
  things being compared, ask what the comparison does with the gaps.**
- The ninth instance was not a measure but a **sample**: rows in the validation log share
  a chain and resolve together, so 903 of them are a few dozen observations. Whenever
  something is counted, ask what the unit of independence actually is before putting an
  interval on it. See `cluster_key` in analytics/calibration.py.
- **A detector defined on "the most recent bar" reports a delisted ticker forever.**
  The twelfth instance, found on the signal scan's first live run: five of fifteen
  signals came from eight symbols that stopped trading between 2024 and early 2026,
  including a 15.6x "volume surge" that was the last day before an acquisition closed,
  which is the highest volume day a ticker ever has. A third of the output was
  archaeology presented as news. `jobs/signals.py` now skips a symbol whose last
  session is more than `MAX_STALE_DAYS` behind **the newest session held anywhere in
  the database** rather than behind today's date, because the calendar does not know
  which weekdays were holidays and a wall clock reference would declare the whole
  universe stale every morning before the price sync lands.
- **A signal that is a state needs an edge; a signal that is an event does not.** A
  squeeze runs a median of 4 sessions and up to 41, so firing while it holds sends six
  alerts for every one thing that happened, and per-session suppression cannot fix that
  because each of those days is legitimately a different day. Fire on the transition.
- **A backtest is the easiest thing here to build so that it always says yes.** Four
  guards, all in `analytics/backtest.py`: entries fill on the **next bar's open** (a
  close-based rule filled at that close is a fantasy); ties between a target and a stop
  go to the **stop**, because intrabar order is unknowable; unfinished trades are
  **dropped**, not marked to market, or the newest weeks fill with truncated winners;
  and every result is measured against a **null**, never against zero.
- **The null is a circular shift of the entry pattern, not fresh random days.** Drawing
  each symbol's entries independently is far too generous on a correlated universe: a
  rule firing across ninety-eight tech names in one week has the variance of roughly one
  observation, and a scattered null has the variance of ninety-eight, so almost anything
  clears it. A common shift preserves the count, the clustering and the co-movement, and
  destroys only the alignment with what the market did next, which is the thing on trial.
- **`effective_sample` pools by calendar block across symbols, not per symbol.** The
  per-symbol key looks more careful and measures nothing: with overlap suppressed,
  consecutive trades on one symbol are already a horizon apart, so it returns the trade
  count. On a real run it said 9,606 where the honest answer was 106.
- **Never compound concurrent trades.** Multiplying every trade's return treats them as
  consecutive stakes on the whole account; a rule firing across many symbols opens many
  positions at once. Doing it produced a yearly total of +2,737,313%. Equity is an equal
  weighted, monthly rebalanced portfolio (`portfolio_periods`), which is a stated capital
  model rather than an implied one.
- **An option backtest refuses realized vol as a stand-in for implied.** The gap between
  them *is* the variance risk premium, so pricing entry at realized vol prices it at fair
  value and the measured edge goes to zero by construction. `require_implied_vol` raises.
  Historical IV exists for two symbols; every other symbol is refused.
- **A sweep's winner is the maximum of a sample of noise.** `sweep` reports the best cell
  against the best-of-N under the same null, and warns when the winner rests on fewer
  than twenty independent blocks, which it usually does: a tighter parameter selects a
  rarer condition, so the best cell is normally the one with the least evidence.
- **Ranking strategies by raw return ranks the holding period, not the rule.** The
  thirteenth instance. The first grid search returned a top twelve in which every single
  candidate was a 63 day hold: three months of market drift dwarfs anything a rule
  contributes, so the ordering was reading the horizon. Candidates are now scored by
  **z against their own null**, which is what makes a 5 day rule and a 63 day rule
  comparable. Once fixed, the top of the list changed completely and became short-horizon
  mean reversion. Whenever candidates differ structurally, normalise before ranking.
- **A search needs a bar that grows with the search.** The best of n candidates reaches
  about `sqrt(2 ln n)` standard deviations above its null when nothing works: 2.4 at
  twenty, 3.0 at eighty. `expected_best_z`. Comparing a winner to zero instead is how a
  grid manufactures a discovery.
- **A holdout must reserve a horizon's margin.** Entries are filtered so a training trade
  cannot settle inside the test period, and the boundary is one calendar date for every
  symbol rather than a per-symbol percentile, which would put different names' holdouts
  in different market regimes.
- **Ten basis points was a placeholder and it was wrong by five times.** Measured with
  Corwin-Schultz across 188 symbols: median 47 bp a round trip, mean 61, and 153 median
  on the uranium names. `cost_model="estimated"` charges each symbol its own spread.
  A flat rate flatters exactly the strategies that trade the thinnest names.
- **Costs do not change an edge, only the take-home.** The null pays what the strategy
  pays, so cost cancels out of a timing comparison and `edge` is identical under either
  model. What moves is `mean_return`. Read both: the first says whether the rule works,
  the second says whether you would keep anything.
- **Nothing reaches `ideas.py` without `Evidence` attached**, and a strategy that fails a
  holdout is marked RETIRED with the reason rather than deleted. The record of what was
  believed and why is what makes the next search less credulous than the last.
- Two filters aimed at the same thing will hide each other. The confounded one in front
  of the principled one does not merely fail to help, it starves the good one of the
  data it needs.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase

**Phases 0 to 9 are complete and committed.** The roadmap ends at 9. Anything further is
new scope and is **not authorized**: ask first.

Authorized since, each with its reasoning in the DECISIONS entry of that date (the
long-form notes that used to live here are archived verbatim at the end of DECISIONS.md,
under "Archived from CLAUDE.md, 2026-10-22"):

| date | scope |
|---|---|
| 2026-09-05 | Journal view; Best plays view |
| 2026-09-07 | broker ledger (`optscan import`, `trades`); bulk price history + `universe.yaml`; Alpaca adapter; Market Chameleon IV history; price chart |
| 2026-09-08 | market signals; backtester; strategy search; universe-wide capture; charts for any symbol + intraday; phone access; home page, search, pinning |
| 2026-09-13 | the short premium study (`optscan premium`, 0-1 DTE QQQ/SPY option tape) |
| 2026-09-17 | iron butterfly in the premium engine; open interest and gamma levels |
| 2026-10-21 | validation metric is return on capital within strategy; `optscan conditions` (forward gamma record); managed vertical spreads + `in_sigmas` in the backtester; overnight entry-edge studies in `scripts/studies/` |
| 2026-10-22 | call debit spread plans on oversold bounce ideas; study harness; `scripts/check.ps1`; generated wiring map (`optscan map`, `map/`), trap tests, CI workflow, `docs/` + `scripts/build_doc.py` |

### Where things stand (2026-10-22)

- **The only validated edge is `oversold_bounce` on the stock**: RSI(14) <= 30, long five
  sessions. Re-confirmed on the 2022-26 holdout at +0.96% over its null, p=0.017.
- **Its call debit spread expression is a lead under forward test, not a finding.** 15
  DTE, long ATM, short 0.84 sigmas above, closed after five sessions: +14.0% dev edge,
  +16.5% holdout, but it missed its pre-registered gate. Robust across a fragility sweep;
  the fill decides profit (+11.7% / -8.8% a trade at 5% / 15% slippage). Plans for it are
  on the ideas list, labelled as a lead. The forward test is the user logging real fills.
- **Nothing else tested has an edge**: vol-timed premium selling, RSI/EMA/level filters on
  the 0-1 DTE tape, indicator-confluence switches, the composite score within strategy.
- **The composite score does not rank within a strategy.** The old "anti-correlated with
  profit" claim was mostly position size; see DECISIONS 2026-10-21.
- **Gamma cannot be backtested** (no historical per-strike open interest exists for this
  project). `optscan conditions` records it forward and **is not yet in the schedule**.
- **Re-run `optscan validate` around late November 2026**, once rows recorded with an IV
  rank (fixed 2026-10-21) have settled.

### Area rules

Rules that only bite when you touch a particular area. One line each; the reasoning is
in the archived notes and the DECISIONS entry of the same date.

**Data and vendors**
- Never omit `expiration_date_gte`/`lte` on Alpaca's `/v2/options/contracts`.
- Alpaca uses two hosts on purpose, and the feed name differs per endpoint.
- One table serves several vendors and never pools them; choose between two vendors'
  series, never merge them. Absent is not zero.
- Market Chameleon files have no symbol column (the symbol is a required argument, never
  inferred) and quote IV30 in vol points; this project stores decimals. A disagreeing
  import is counted and refused, never applied.
- A symbol a vendor returns nothing for is named, never dropped.
- Intraday bars are keyed by epoch seconds and daily bars by ISO date, never mixed; do
  not infer which from the timestamp (Alpaca stamps daily bars at 04:00Z).
- `days` on the history endpoint is a bar count, not calendar days.
- Intraday comes from `get_intraday_provider`, not `OPTSCAN_PROVIDER`.
- A symbol's IV history comes from `load_implied`, never from the price series' source.

**Ranking and scoring**
- A rank must read today's vol at the history's own tenor (trap ten).
- A cross-symbol ranking may only use components every candidate has (trap eleven).
- Compare a result per dollar risked and within strategy, never in raw dollars across
  positions of different size.

**Screens, charts and the dashboard**
- The dashboard route evaluates signals but never delivers or records them.
- The watchlist is the only thing the API writes; pinning does not capture a chain and
  unpinning deletes nothing.
- Keep three data states distinct: in a group, has price history, has a captured chain.
- Never call `applyOptions` from a `useEffect` of its own.
- A partial moving average is never drawn. No floating tooltip, no vertical gridlines,
  the wheel belongs to the page, panels carry no border, the accent stays blue.
- Colour is never the only carrier of meaning.

**Jobs and operations**
- Recording has to keep running: a candidate not logged when scored cannot be settled
  later. Same for the 15:45 snapshot, which is the IV history's only writer.
- Capturing is not screening, and they stay separate. `--force` writes stale marks under
  a session that has not happened.
- The backup default is on the same drive as the data; point `OPTSCAN_BACKUP_DIR`
  elsewhere for a real backup.
- For phone access on the shared network, bind a private-mesh interface with `--host`,
  never `--lan`. The firewall command is printed, never run.

**The broker ledger**
- Rows are stored raw and positions are derived. An unknown transaction code raises.
- The trailing `S` on an expiring leg is captured and never decoded.

**Research**
- Option payoffs held to expiry are exact; only the entry credit is modelled. A managed
  exit is a modelled exit, and a trigger fills on the session after it fires.
- The tenor band is 15 to 60 DTE; a thirty day IV cannot price a 7 or 90 day contract.
- Pre-register hypotheses, thresholds and the holdout gate before running; use
  `scripts/studies/harness.py`.
- Market-neutralising returns was tried and rejected on measurement (the shift null
  already neutralises the market); do not rebuild it without new evidence.
- The 184 cell strategy search found nothing that survived its holdout; its finalists
  are recorded as RETIRED in `jobs/ideas.py`, not deleted.

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
- **Outcomes have resolved, but not enough of them to claim anything.** The first real
  `optscan resolve` runs settled 14 clusters over 3 settlement dates (the Journal findings
  above), under `MIN_CLUSTERS_FOR_A_CLAIM` (20), so the report still draws no conclusion.
  "Resolved but below the threshold" is not "never resolved": the calibration table
  prints from these rows today.
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
