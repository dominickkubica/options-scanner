# Notes: what the code cannot say about itself

One entry per item, keyed the way `map/generated/` names it. Every table and every
scheduled job has one; anything else gets one only when its purpose or a trap is not
obvious from the generated facts.

Each entry is pinned. `fact:` is a hash of the item's generated facts, so when its
writers, readers, calls or schedule change, `tests/test_map.py` fails until someone
re-reads the note and runs `optscan map stamp <key>`. A trap is pinned to the test that
guards it (`test: <file>::<name>`), or to `test: none` when no test can.

Present tense only. Why something came to be lives in `DECISIONS.md`.

## Tables

### table:watchlist
fact: 5234b4414f8a
The symbols the 15:45 capture walks. Pinned from the CLI or the dashboard.

### table:snapshot_run
fact: 35d7f6475606
The manifest of every chain capture: one row per symbol per run, with its outcome.

### table:vendor_daily
fact: 04560d0980a2
Daily OHLCV per symbol per source. Market Chameleon rows also carry `iv30` and call/put
open interest totals; Alpaca rows do not, which is why IV is never read through the
price source (see trap:iv-history-source).

### table:vendor_import
fact: 3cc9db912bb1
One row per imported vendor file, so a re-import of the same file is recognised.

### table:vol_index
fact: 08aa322bd21a
Closes of Cboe's VIX, VXN and GVZ: an always-current IV rank for SPY, QQQ and GLD.

### table:scan_run
fact: ef036a96f783
One row per `optscan record` run.

### table:opportunity_log
fact: 38b3bcf340cc
Every candidate the screen scored, as scored, for the validation study. Cannot be
re-logged after the fact, which is why the backup job exists.

### table:opportunity_outcome
fact: 38b3bcf340cc
The settlement of each logged candidate once its expiry has passed.

### table:position
fact: c2684b94cfc6
Positions entered by hand with `optscan position add`. A position opened from a spread
plan carries `exit_on` (the close `manage` raises on the day) and `plan` (the plan as
printed), which `optscan forward-test` reads back against the fill.

### table:position_leg
fact: c2684b94cfc6
The legs of each hand-entered position.

### table:alert_sent
fact: c2684b94cfc6
Position alerts already delivered, so each condition alerts once.

### table:signal_sent
fact: b59e78021362
Market signals already delivered, so each fires once per symbol, kind and session.

### table:broker_import
fact: 787a724cd4cb
One row per imported Robinhood activity file.

### table:broker_txn
fact: d709051b4770
The Robinhood ledger, keyed on each row's own contents so overlapping exports add only
what is new.

### table:fill_time
fact: c5ef9f7046e5
Fill timestamps from Robinhood order history (`optscan import-fill-times`, run by the
user because the login is interactive).

### table:journal_annotation
fact: 8e1070aedbc4
What the trader adds to a position in the journal: tags, setup, notes.

### table:journal_screenshot
fact: 8e1070aedbc4
Screenshot records attached to journal positions; the images stay on this machine.

### table:market_regime
fact: 8e1070aedbc4
VIX closes fetched for the journal's regime labels.

### table:meta
fact: 26a33fad72ca
Key/value settings, such as the journal's starting balance.

### table:job_run
fact: fdd94fa96fbe
Every scheduled job run and how it ended; `optscan health` reads it.

### table:holdout_use
fact: f2e04f30ed86
Every look at a held-out period, so a holdout is spent knowingly.

### table:daily_condition
fact: 65799ccff487
Per-symbol daily gamma regime and indicators, recorded forward by `optscan conditions`.
No vendor sells historical per-strike open interest, so this record is the only gamma
history there will ever be. Written by the `conditions` job once the user installs it.

## Scheduled jobs

### job:snapshot
fact: b9ea98179c67
Captures the watchlist's chains. The IV history has exactly one writer, this job, and a
missed day is permanent.

### job:capture
fact: c8dab7f9bb1f
Captures every universe symbol's chain. Off by default: needs Alpaca keys and takes
about nine minutes.

### job:prices
fact: 5d82635e6e59
Refreshes daily bars. Off by default because it needs Alpaca keys; without it, trade
ideas go stale (the ideas list skips symbols whose last bar is old).

### job:record
fact: 2105a2a6272f
Logs every scored candidate into `opportunity_log`. Runs after the capture because it
scores the stored snapshot.

### job:signals
fact: 8f4de6101cdd
Evaluates stored bars for level breaks, squeezes and volume surges. Off by default
because it depends on `prices`.

### job:conditions
fact: a0939e1837bf
Records the forward gamma history into `daily_condition` at 16:25. Off by default
(chain fetches against a vendor that throttles silently); installed with
`optscan schedule --install conditions`, which the user runs themselves.

### job:backup
fact: 510f9e04b569
Copies the two things that cannot be rebuilt: the parquet captures and the SQLite file.

### job:resolve
fact: 431f15fb8842
Settles logged candidates whose expiry has passed, before the open.

### job:manage
fact: a6cc2617e6b0
Marks open positions and delivers alerts during the session. Off by default: about 26
provider calls a session against a vendor that throttles silently.

## Routes and caches

### route:/symbols/{symbol}/gex
fact: c89989a1452e
Open interest describes the previous close (OCC publishes once a day). Spot falls back
to the chain's underlying when no live quote is available. Expired expiries are skipped.

### route:/gaps
fact: 27c4f34889f9
Served, but no dashboard file calls it; reachable from the API only.

### cache:_SOLVED
fact: 68051199d16a
Holds a solved capture until a newer capture replaces it. After importing data or
changing backend code, restart `optscan serve`.

## Traps

### trap:iv-history-source
test: tests/test_traps.py::test_iv_history_is_never_read_through_the_price_source
A symbol's IV history comes from `jobs/backtest.load_implied`. `preferred_source` names
the price source, which is Alpaca for most symbols, and Alpaca has no IV30.

### trap:scorer-without-iv-history
test: tests/test_traps.py::test_every_scorer_call_states_its_iv_history
`screener/context.analyze_snapshot` without `iv_history` scores with no IV rank and
says nothing. Every caller passes it, or passes `iv_history=None` deliberately.

### trap:study-symbol-loss
test: tests/test_traps.py::test_a_study_refuses_to_run_on_a_fraction_of_its_symbols
A study that silently loses most of its symbols produces a table that looks like any
other. `scripts/studies/harness.py` raises when more than half are lost.

### trap:live-vs-oi-chain
test: none
A chain for display is `deps.live_chain` (open session only, 20 s). A chain for open
interest is `deps.oi_chain` (any time, 300 s). Neither writes storage. Behavioural, not
checkable by a static test.

### trap:what-can-be-backtested
test: none
Underlying rules on 294 symbols; single options and spreads at 15-60 DTE on the IV30
symbols (`TENOR_BAND`); 0-1 DTE on QQQ/SPY from the option tape. Not gamma, walls, or
anything needing historical per-strike open interest, and nothing at 7 or 90 DTE. A
limit of the vendors, not of the code.

### trap:two-servers-one-port
test: none
One server on 127.0.0.1 and another on 0.0.0.0 can share port 8000, and localhost then
reaches the old build. Check with `Get-NetTCPConnection -LocalPort 8000`.
