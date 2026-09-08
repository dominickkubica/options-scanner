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
  imports/          broker statement parsers, one module per broker
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
venv\Scripts\python -m optscan import <csv> # take in a broker activity export
venv\Scripts\python -m optscan import-history <csv|dir> # vendor daily history, with its IV30
venv\Scripts\python -m optscan history # what downloaded vendor history is held
venv\Scripts\python -m optscan prices sync [GROUP...] # bulk daily bars for a universe
venv\Scripts\python -m optscan prices groups # the curated symbol groups and their age
venv\Scripts\python -m optscan trades  # realized results from imported statements
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
- Two filters aimed at the same thing will hide each other. The confounded one in front
  of the principled one does not merely fail to help, it starves the good one of the
  data it needs.

## Non-goals
- No auto-execution of trades. This tool ranks and displays, it does not place orders.
- No claims of predictive accuracy that have not been validated in Phase 8.

## Current phase

**Phases 0 to 9 are complete and committed.** The roadmap ends at 9. Anything further is
new scope and is **not authorized**: ask first.

**Exception, authorized 2026-09-07: the broker ledger.** `optscan import` reads a
Robinhood activity export, `optscan trades` reports what the account actually did.
`models/broker.py`, `imports/robinhood.py`, `analytics/ledger.py`,
`storage/ledger.py`, migration 6. Full reasoning in the DECISIONS entry of that date.

- **Rows are stored raw and positions are derived.** Exports overlap, so a re-import
  must be a no-op. Identity is `(source, digest, dup_index)`: the index is there
  because two identical fills on one day are two trades and a hash cannot tell them
  apart.
- **The trailing `S` on an expiring leg is captured and never decoded.** It sat on the
  bought leg in all three reference spreads and on the higher strike in all three, and
  three cases cannot separate those rules. An expiration's direction comes from the
  holding the ledger says was open. `signed_quantity` returns None rather than guessing.
- **Fees are measured, options only.** 4 cents a contract on buys, 6 on sells. On
  shares the same arithmetic went negative on 14 of 34 rows because Robinhood rounds the
  displayed price while the amount is exact, so equity fees are None, not zero.
- **An unknown transaction code raises.** Assignment and exercise move real contracts,
  and a skipped row is a profit figure with a hole and nothing to say so.

**Exception, authorized 2026-09-08: a home page, search, and pinning.** `catalogue.py`,
`api/routers/catalogue.py`, `views/Home.jsx`, `views/Browse.jsx`,
`components/SymbolSearch.jsx`. Home is now the default view. Full reasoning in the
DECISIONS entry of that date.

- **Three data states, never conflated.** In a group (a label), has price history
  (chartable, **not screenable**), has captured chains (the only screenable one).
  Currently 293 / 293 / **6**. Every search row shows a status phrase, not a checkmark.
- **Pinning does not capture a chain.** It makes the *next* snapshot run fetch one, and
  every add says so. A pinned card with no capture is drawn **outlined rather than
  filled** - the same signal Best plays uses for a blocked near miss, reused on purpose
  so the app has one vocabulary for "real, but not ready".
- **Unpinning deletes nothing.** A chain from a day that has passed cannot be captured
  again.
- **The watchlist is the only thing this API writes.** Justified because it decides what
  the capture job fetches; idempotent, so a double click is not an error.
- **Two vendors on one symbol broke two numbers, both found by reading the page.** The
  daily change was computed between two vendors' *same* session and rendered +0.00% on
  exactly AAPL and QQQ, the only dual-sourced symbols. Session counts were summed across
  vendors, reporting 5,699 for a symbol with 3,188. Pick one source first;
  `COUNT(DISTINCT session_date)`. Neither is visible in a test that seeds one vendor.

**Not done, and next:** pinning is now one click, and Best plays reports the top
candidate across the watchlist, so a large watchlist makes that a maximum over far more
draws and the list will look better with nothing having improved. The guard belongs in
the ranking, not the button.

**Exception, authorized 2026-09-07: bulk price history and a symbol universe.**
`optscan prices sync` fills `vendor_daily` for many symbols at once; `universe.yaml`
and `universe.py` hold the named groups. Migration 8. First run stored **293 of 294
symbols, 693,974 sessions, 2016-09-09 to 2026-09-04.** Full reasoning in the DECISIONS
entry of that date.

- **The same table serves both vendors and never pools them.** AAPL holds 2,511 Alpaca
  sessions and 3,188 Market Chameleon ones side by side, keyed
  `(source, symbol, session_date)`.
- **Three volume fields, not one.** Shares, trade count and VWAP. Volume alone cannot
  tell 30 million shares in 500,000 prints from the same volume in 5,000;
  `average_trade_size` is the ratio and needs the count.
- **Batching is the feature.** 58 symbols in one request in 0.7s against ~35s the naive
  way. `get_daily_bars_bulk` sits outside `MarketDataProvider` on purpose.
- **A symbol that returns nothing is named, never dropped.** The vendor omits what it
  does not know with no error. A hole in a price history is invisible in every chart
  drawn over it. **No OTC name can ever sync** (`403 ... querying OTC data`).
- **`universe.yaml` is curated and says so.** No vendor here publishes sector or index
  data, so somebody typed these lists. `sp500_large` is not the S&P 500: it is the
  constituents somebody remembered to type, which is exactly the set that did not get
  dropped for performing badly. Every aggregate over it carries that survivorship.

**Not done, and the thing to weigh before doing it:** none of this is pointed at the
screener, and the watchlist is still six symbols. Expanding the *screen* to hundreds of
symbols is a different decision, because Best plays reports the top candidate and the
best of 600 symbols is a maximum over 100x more draws than the best of 6. That is a
selection effect that will make the list look better with nothing having improved.
`calibration.py` already holds the cluster reasoning for this shape of problem and it
has not been applied here.

**Exception, authorized 2026-09-07: the Alpaca adapter.** `providers/alpaca.py`,
selected with `OPTSCAN_PROVIDER=alpaca` and a key pair in `.env`. Free Basic plan.
Full reasoning in the DECISIONS entry of that date; five things there were measured
against the live API because the documentation is wrong about them and every one fails
silently with a 200:

- **Two hosts, on purpose.** Data is on `data.alpaca.markets`; **open interest exists
  only on the trading host** at `/v2/options/contracts`, so a chain joins the two.
- **The feed name differs per endpoint.** Bars take `sip`, snapshots take
  `delayed_sip`, and each is an error on the other. `feed=iex` is the trap: 200 OK with
  1.8% of consolidated volume and no indication of it.
- **Never omit `expiration_date_gte`/`lte` on `/v2/options/contracts`.** Without them
  it answers for exactly 4 expiries, 200, no page token, and paging at limit=100 walks
  17 pages to the same truncated set. With bounds, two years gives 33.
- **Greeks and IV are on the free feed.** Stored as `vendor_iv`, still unused: this
  project solves its own vol, same rule as Tradier's unusable `mid_iv`.
- **`403 OPRA agreement is not signed`** is a signature, not a subscription, and is
  reported separately from a bad key.

**What it cannot do.** No historical option quotes at all (`/v1beta1/options/quotes` is
a 404; only `/latest` exists), and historical option trades are recent-only. Option
*bars* do go back to ~2024-01-18 including minute bars on 0DTE contracts, so historical
option data is **trade priced, never mid priced**. Historical *stock* NBBO quotes, by
contrast, are full tick depth. Stocks are richly served; options are not.

**`get_events` is half a calendar and says so.** Ex-dividend comes from
`/v1/corporate-actions` and serves `exclude_early_assignment_risk`. `earnings_date`
stays None and is never guessed, because `exclude_earnings` is the heavier filter and a
screen that believes it checked earnings and did not is worse than one that knows it
could not.

**Exception, authorized 2026-09-07: downloaded vendor volatility history.** `optscan
import-history` reads a Market Chameleon daily export, `optscan history` reports what is
held. `models/vendor.py`, `imports/marketchameleon.py`, `storage/vendor.py`, migration 7.
Full reasoning in the DECISIONS entry of that date.

**This is what finally makes IV rank real.** One export is 3,188 sessions back to 2014
carrying the vendor's own IV30, against the 8 daily captures this project had managed
for itself. `component_iv_rank` was null on all 10,020 recorded candidates and is not
any more: QQQ now ranks 0.181 at percentile 0.194 on 252 observations, confidence high.
Held for QQQ and AAPL only. **The trial allows 2 downloads per 24 hours; SPY and IWM are
the next two.**

Four rules came out of it and none are cosmetic:

- **The format has no symbol column.** The ticker is in the filename and nowhere else,
  so a renamed file imports the wrong instrument under the right name and nothing
  contradicts it. The symbol is a required argument, never inferred silently, and an
  import that disagrees with stored sessions is **counted and refused, never applied**.
  Verified by importing AAPL as QQQ: 3,188 of 3,188 conflicted and nothing was written.
- **IV30 is in vol points and this project stores decimals.** 17.19 becomes 0.1719, once,
  in the parser, with a test on it. A hundredfold vol error is obvious in a payoff
  diagram and invisible in a rank, which is the only place it goes.
- **Two vendors' series are chosen between, never merged**, and the *current* value must
  come from the same vendor as the range it is ranked inside. Ranking a locally solved
  vol in a downloaded range is the pooling rule broken in a new place: measured, the two
  disagree by -3.2% on QQQ and +2.9% on AAPL, in opposite directions, and QQQ's rank came
  out 0.14 that way against 0.181 done properly.
- **Absent is not zero.** Three sessions have no IV30 in every ticker (they are holes in
  the vendor's pipeline, not the symbol's), and open interest starts 2018-01-30.

**Trap ten: a rank must read today's vol at the history's own tenor.** `analyze_snapshot`
used to rank `expiries[0].atm_iv`, the front expiry, against a 30 day history. Short
dated ATM vol is mechanically elevated: the front expiry solves to 43 vol points on QQQ
and 83 on AAPL at 0 DTE against 30 day points of 17.2 and 25.5. It never fired only
because no history was long enough to produce a rank, so importing one is exactly what
would have made it live. `atm_iv_near_dte` returns None outside a half-to-double band
rather than the nearest expiry, and `SymbolAnalysis.iv_rank_note` carries the reason so
the gauge explains itself instead of rendering blank.

**Trap eleven: a cross-symbol ranking may only use components every candidate has.**
`composite` renormalizes a missing component onto the others, which is right within one
symbol and badly wrong across them, because it silently replaces the missing value with
the average of that candidate's other components. Measured on the live config: **below an
IV rank of 0.939, having imported a history LOWERS a symbol's score.** QQQ's honest 0.18
cost it 0.152 and dropped it out of Best plays for the sole reason that its data exists.

`scoring.harmonize_scores` rescores a to-be-ranked list over the **intersection** of what
its members share, and every affected row says what was dropped. It is called from the
scan router and the CLI display, and deliberately **not** from `jobs/validate.py`: a
recorded score must depend only on the candidate, never on which symbols shared its
batch, or the study calibrates a number that moves for reasons the market did not.
Downloading the other four tickers is what widens the intersection back.

**What it does not do.** IV30 is one number, not a surface. No skew, no term structure,
no per strike vol. It supports a rank, an IV/HV comparison and vol regime context; it
cannot price a 20 delta put and cannot touch 0DTE, so it does not on its own enable a
backtest with modelled credits.

**The screener's DTE band is now 0 to 45**, in `screen.yaml` and in the `DteFilter`
defaults so the two cannot drift. It was 21 to 60, and the imported ledger showed 337 of
380 option legs expiring the day they were opened: the screen had never once surfaced a
trade this account would take.

**Scoring below about five days is not trustworthy yet, and widening the band did not
cause it.** `annualized_return` is undefined at 0 DTE so those candidates fall into the
branch written for undefined risk positions and take a 0.75 discount for it, and
`min_annualized_return` stops applying there because the value it compares against is
None.

**Measured against all 10,020 recorded candidates, three of the five score components
carry no information.** `component_premium` is exactly 1.000 for 88.6% of rows because
the annualized return ramp ceiling of 0.25 is about 30x below what a credit spread
produces. `component_event_risk` is 1.000 for 100% of rows, stdev 0.0000, because
`exclude_earnings` removes every case it would penalise before scoring runs. `iv_rank`
was null throughout, and is now real for any symbol with an imported history (see
above). Only liquidity and probability actually vary, so the composite is
roughly 5/8 liquidity and 3/8 probability of profit, which is the most likely
explanation for the score being anti-correlated with profit. **Not yet fixed.**

**Exception, authorized 2026-09-07: the price chart.** `frontend/src/components/chart/`
replaces the old `Candles.jsx`. Intervals 1D, 1W and 1M, windows per interval, candles
or area, and MA20/MA50/MA200 toggleable and off by default. Full reasoning in the
DECISIONS entry of the same date. Five things there should survive future edits:

- **No floating tooltip.** Hovered values go in the fixed header above the canvas. A
  tooltip covers the candles the reader is pointing at.
- **No vertical gridlines, no axis borders, and the price axis is locked to autoscale.**
  The lock is not fussiness: dragging the axis can flatten a thirty percent move into a
  straight line, which is the chart lying because of a slip of the mouse.
- **The wheel belongs to the page.** A chart may capture it when it owns the screen.
  This one is a panel among five, and capturing it blocked page scrolling and silently
  drifted the view off the latest bar.
- **`days` on the history endpoint is a bar count, not calendar days.** Measured:
  `days=730` returns 730 sessions starting 2023-10-09. Window sizes come from
  `SESSIONS_PER_YEAR` so the label matches the span. The first version got this wrong in
  the same direction on every window, which is why it looked consistent.
- **A partial moving average is never drawn**, and an indicator that cannot draw says
  why on its chip instead of disappearing. Same rule as the vol solver and IV rank.

Indicators live in a registry at `chart/indicators.js`. Adding one means adding an entry
there and touching nothing else; an entry may declare several plots, which is what lets
a Bollinger band or MACD arrive without reopening the chart component. Intraday
intervals are deliberately deferred, see the DECISIONS entry.

**Visual pass, 2026-09-05.** Surfaces, radii, spacing and type were re-based on
measured values rather than taste: page `#101626`, panel `#1d2333`, raised `#2e3446`,
12px on cards, 8px on controls, Inter at 14px/500 self hosted through
`@fontsource-variable/inter` so a cold dashboard has no font request and no flash.

Two rules came out of it and should survive future edits:

- **Panels carry no border.** Separation is surface contrast plus space. Four panels a
  screen meant four more lines competing with the numbers inside them, and removing
  them is most of what made the app feel calmer. The remaining consequence is worth
  keeping: a blocked near-miss card is now the only card on any screen with a border
  at all, which makes that distinction stronger than when everything had one.
- **The accent stays blue.** Green already means profit here, so a green primary
  button sitting next to a green P/L figure would be one signal doing two jobs.

`.btn.primary` is now filled rather than outlined; before this every button in the app
was the same outlined grey and the one that mattered had to be found by reading.

**Exception, authorized 2026-09-05: the Journal view.** A trade journal's report
surface - KPI tiles, equity curve, calendar P/L, and breakdowns by strategy, symbol,
DTE and score - built over settled `opportunity_outcome` rows at `GET /api/journal`.

It is **not** a record of trades taken. Nothing in it has been to a broker, and the
banner above the numbers says so. It measures the screen held to expiry, with no fill,
no slippage and no early management. `analytics/journal.py` carries the reasoning; the
short version is that a calendar and an equity curve are the two most persuasive
objects this app can draw and neither knows what it is drawing, so every aggregate
carries its cluster count and `reportable` is false until the sample clears the
`calibration.py` minimum.

Two findings from the first real run, both worth re-checking as the sample grows:

- **The composite score is currently anti-correlated with profit.** Mean profit by
  score band runs +$478, +$266, +$188, +$102 from the lowest band to the highest. At 14
  clusters over 3 settlement dates that is not a finding, but it is the opposite of
  what the score claims and the Best plays view ranks on it.
- **14 clusters sit on only 3 settlement dates.** Six symbols expiring on one Friday
  share one market move, so the effective sample is nearer 3 than 14. The report states
  both numbers rather than letting the cluster count stand alone.

**Exception, authorized 2026-09-05: the Best plays view.** A landing screen that ranks
the whole watchlist instead of the selected symbol, plus near miss collection behind
`GET /api/scan?near_miss=true`. Three constraints hold it together and none are
cosmetic:

- It scans the watchlist, never the sidebar selection. "Best play available" that
  silently meant "best in NVDA" would be the most misleading screen in the app.
- Near misses are scored by the same function as passing candidates and **routinely
  outscore them**, because the gate that blocked them is not an input to the score. In
  the first run against real captures the top blocked candidate scored 0.976 against
  0.956 for the best that passed. They are drawn outlined and dimmed and always carry
  their blocker, and that separation is load bearing, not styling.
- The score bar is length only. A colour ramp would assert that some threshold is good,
  and `optscan validate` is still under its own cluster minimum, so no threshold here
  has earned that.

`enters_screen_in_days` is populated only for a DTE ceiling blocker, because that is the
only gate that clears by waiting. Everything else needs the market to move, and dating
that would be inventing a number.

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
