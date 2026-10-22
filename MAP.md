# MAP: how the app is wired

Where data comes from, where it is stored, what writes and reads it, and how it reaches
the screen. Read this before tracing anything by grep.

**Keep it current.** Any change that adds, removes, or rewires a module, table, route,
view, scheduled job, cache, or data directory updates this file in the same commit.
`tests/test_map.py` enforces the inventory part — every module, table, route and view
must appear here — so the suite fails until a new one is listed. The arrows and the
prose are not enforced; keeping those true is on whoever changes the wiring.

Last full audit: 2026-10-22, against `7d527e9`.

---

## 1. The whole thing on one screen

```
 VENDORS                      JOBS (scheduled / CLI)          STORAGE (data/)
 ───────                      ──────────────────────          ───────────────
 yfinance ──chains,OI,IV────▶ snapshot (15:45) ─────────────▶ snapshots/  (parquet)
          ──expiries,chains─▶ (on demand: deps.oi_chain) ──── in-memory caches only
 Alpaca ───daily bars───────▶ prices sync ──────────────────▶ vendor_daily (sqlite)
        ───minute opt trades▶ premium sync ─────────────────▶ option_tape/ (parquet)
        ───live quotes (delayed SIP) ──▶ deps.live_quotes ─── in-memory cache
 Market Chameleon CSV ──────▶ import-history (manual) ──────▶ vendor_daily (iv30, OI totals)
 Cboe ^VIX ^VXN ^GVZ ───────▶ vol_indices (in prices) ──────▶ vol_index
 Robinhood CSV / orders ────▶ import, import-fill-times ────▶ broker_txn, fill_time

 STORAGE ─▶ screener/ + analytics/ (pure math) ─▶ api/ (FastAPI, caches) ─▶ frontend/ (React)
        └─▶ jobs: record, resolve, validate, signals, ideas, backtest, premium, conditions
                    └─▶ alerts.py ─▶ data/alerts.jsonl (+ configured sinks)
```

The rule this diagram encodes: **analytics/ and screener/ never do I/O**. Vendors are
reached only through `providers/`, storage only through `storage/`, and both are
orchestrated by `jobs/` (batch) or `api/deps.py` (requests).

---

## 2. Vendors

| vendor | what it gives | through | used for |
|---|---|---|---|
| yfinance | option chains (bid/ask, OI, IV), expiries, quotes, events | `providers/yfinance_provider.py` | nightly capture, gamma panel, spread plans, events |
| Alpaca (free Basic) | daily + minute stock bars, delayed-SIP live quotes, historical option **trades** (no quotes) | `providers/alpaca.py` | price history (294 symbols), live quotes, the 0–1 DTE option tape |
| Market Chameleon | daily CSV export: IV30, call/put volume, call/put OI **totals** | `imports/marketchameleon.py` (manual download) | IV rank, the option backtester's IV (24 symbols, 2014 on) |
| Cboe indices | VIX, VXN, GVZ closes (via Yahoo `^VIX` etc.) | `jobs/vol_indices.py` | always-current IV rank for SPY, QQQ, GLD |
| Robinhood | activity CSV export; order history for fill times | `imports/robinhood.py`, `imports/fill_times.py`, `providers/robinhood_orders.py` | the journal and ledger |

Index symbols alias for the chain vendor: `SPX→^SPX`, `XSP→^XSP` etc., in
`api/deps.py` `INDEX_VENDOR_SYMBOLS` / `vendor_symbol()`.

**No vendor here has historical per-strike open interest or historical option quotes.**
That single fact decides what can be backtested (see section 9).

---

## 3. Storage

### SQLite: `data/optscan.sqlite` (schema in `storage/db.py`, `MIGRATIONS`, 14 so far)

| table | written by | read by | holds |
|---|---|---|---|
| `watchlist` | `storage/db.py` (CLI `watchlist`, API pin) | `storage/db.py`, `api/routers/signals.py` | symbols the nightly capture walks |
| `snapshot_run` | `storage/db.py` | `storage/db.py` | manifest of every chain capture |
| `vendor_daily` | `storage/vendor.py` (prices sync, import-history) | catalogue, backtest, ideas, signals, vendor | daily OHLCV per source; MC rows add `iv30` and OI totals |
| `vendor_import` | `storage/vendor.py` | `storage/vendor.py` | one row per imported file |
| `vol_index` | `storage/vol_index.py` | `storage/vol_index.py` | Cboe vol index closes |
| `scan_run` | `storage/validation.py` | `storage/validation.py` | each `record` run |
| `opportunity_log` | `storage/validation.py` | `jobs/validate.py`, `storage/validation.py` | every scored candidate, for the validation study |
| `opportunity_outcome` | `storage/validation.py` | `jobs/validate.py`, `storage/validation.py` | settlement of each logged candidate |
| `position`, `position_leg` | `storage/positions.py` | `storage/positions.py` | positions entered by hand |
| `alert_sent` | `storage/positions.py` | `storage/positions.py` | once-per-condition position alerts |
| `signal_sent` | `storage/signals.py` | `storage/signals.py` | once-per-session market signals |
| `broker_import`, `broker_txn` | `storage/ledger.py` (+ `storage/db.py` migration 13) | ledger, `api/routers/journal.py` | the Robinhood ledger |
| `fill_time` | `storage/journal_book.py` | `storage/journal_book.py` | fill timestamps from order history |
| `journal_annotation`, `journal_screenshot`, `market_regime` | `storage/journal_book.py` | `storage/journal_book.py` | what the trader adds to the journal |
| `meta` | `storage/db.py`, `storage/journal_book.py` | `storage/jobs.py`, `storage/journal_book.py` | key/value settings (starting balance etc.) |
| `job_run` | `storage/jobs.py` | `storage/jobs.py` | every scheduled job run and how it ended |
| `holdout_use` | `storage/holdout.py` | `storage/holdout.py` | every look at a held-out period |
| `daily_condition` | `storage/conditions.py` | `storage/conditions.py` | per-symbol daily gamma + indicators, recorded forward |

### Files under `data/`

| path | written by | read by | holds |
|---|---|---|---|
| `snapshots/symbol=X/session_date=D/*.parquet` | `jobs/snapshot.py` via `storage/snapshots.py` | `jobs/load.py`, `screener/history.py`, `api/deps.py` | full captured chains; **the IV history has exactly one writer, the 15:45 job** |
| `option_tape/underlying=X/session=D/` | `jobs/option_tape.py` via `storage/option_tape.py` | `jobs/premium.py` | QQQ/SPY 0–1 DTE option minute trades, 2024-02 on |
| `vendor/marketchameleon/` | nobody: placed by hand | nobody | raw Market Chameleon CSVs kept for reference; the import reads from wherever the file is passed |
| `alerts.jsonl` | `alerts.py` | the user | delivered alerts |
| `logs/` | `logging.py` | the user | structured logs |

`jobs/backup.py` backs up the two things that cannot be rebuilt: the parquet snapshots (no vendor sells a past chain) and the SQLite file (the validation log cannot be re-logged).

---

## 4. Scheduled jobs (`jobs/schedule.py`; the user installs them with `optscan schedule --install`)

| key | when (market time) | runs | writes |
|---|---|---|---|
| `snapshot` | 15:45 | `jobs/snapshot.py` on the watchlist | `snapshots/`, `snapshot_run` |
| `capture` | snapshot + 5 min | universe-wide chain capture | `snapshots/` |
| `prices` | snapshot + 20 min | `jobs/prices.py` (+ vol indices) | `vendor_daily`, `vol_index` |
| `signals` | snapshot + 35 min | `jobs/signals.py` | `signal_sent`, alerts |
| `record` | record time | `jobs/validate.py` `run_record` | `scan_run`, `opportunity_log` |
| `backup` | record + 15 min | `jobs/backup.py` | backup copies |
| `resolve` | 08:00 | `jobs/validate.py` `run_resolve` | `opportunity_outcome` |
| `manage` | market open | `jobs/manage.py` | `alert_sent`, alerts |

**Gap:** `conditions` (`jobs/conditions.py`) is **not scheduled yet**. Until it is, the
forward gamma record only grows when someone runs `optscan conditions` by hand.

---

## 5. CLI (`cli.py`) → job

| command | module |
|---|---|
| `snapshot`, `status`, `config` | `jobs/snapshot.py`, `config.py` |
| `scan` | `jobs/scan.py` → `screener/scan.py` |
| `watchlist add/remove/list`, `groups` | `storage/db.py`, `universe.py` |
| `prices sync` | `jobs/prices.py` |
| `import` / `trades` | `imports/robinhood.py`, `analytics/ledger.py` |
| `import-history`, `history` | `imports/marketchameleon.py`, `storage/vendor.py` |
| `import-fill-times` | `imports/fill_times.py` |
| `position add/close/delete/list`, `manage` | `storage/positions.py`, `jobs/manage.py` |
| `record`, `resolve`, `validate` | `jobs/validate.py`, `analytics/calibration.py` |
| `signals` | `jobs/signals.py` |
| `ideas` | `jobs/ideas.py` (+ `analytics/bounce_spread.py` plans) |
| `backtest`, `search` | `jobs/backtest.py`, `jobs/search.py` |
| `premium sync/study/reconcile` | `jobs/option_tape.py`, `jobs/premium.py` |
| `conditions` | `jobs/conditions.py` |
| `schedule`, `health`, `backup` | `jobs/schedule.py`, `jobs/health.py`, `jobs/backup.py` |
| `serve`, `shortcut` | `api/app.py`, `jobs/launcher.py` |

---

## 6. API (`api/`, all under `/api`) and what feeds each route

| route | router | main inputs |
|---|---|---|
| `/health` | `health.py` | `storage/jobs.py`, snapshots |
| `/home`, `/watchlist`, `/watchlist/{symbol}` | `watchlist.py` | `storage/db.py`, `deps.live_quotes` |
| `/catalogue` | `catalogue.py` | `catalogue.py`, `vendor_daily` |
| `/quotes`, `/refresh` | `quotes.py`, `refresh.py` | `deps.live_quotes`, `deps.clear_live_caches` |
| `/symbols/{symbol}`, `/symbols/{symbol}/chain`, `/symbols/{symbol}/history`, `/symbols/{symbol}/news` | `symbols.py` | `deps.solved_symbol`, `deps.price_history`, `providers.get_news_provider` |
| `/symbols/{symbol}/levels` | `levels.py` | `analytics/levels.py`, `analytics/projection.py`, solved snapshot |
| `/symbols/{symbol}/gex` | `levels.py` | `deps.oi_expiries`/`oi_chain`, `analytics/gex.py` |
| `/scan`, `/gaps` | `scan.py` | `screener/`, solved snapshots |
| `/payoff` | `payoff.py` | `analytics/payoff.py` |
| `/positions` | `positions.py` | `storage/positions.py`, `analytics/portfolio.py` |
| `/live/status`, `/live/stream` | `live.py` | `live/hub.py` (SSE) |
| `/signals`, `/signals/recent` | `signals.py` | `analytics/signals.py`, `signal_sent` |
| `/ideas` | `ideas.py` | `jobs/ideas.py`, spread plans via `deps.oi_chain` |
| `/backtest`, `/backtest/rules` | `backtest.py` | `jobs/backtest.py` |
| `/journal`, `/journal/annotation`, `/journal/balance`, `/journal/export.csv`, `/journal/import`, `/journal/regime`, `/journal/screenshot`, `/journal/screenshot/{shot_id}` | `journal.py` | ledger tables, `analytics/journal.py`, `analytics/insights.py` |

Wire formats live in `api/schemas.py`; domain-to-wire conversion in `api/views.py`.

### Caches (`api/deps.py`) — why the dashboard can show stale data

| cache | key | TTL | cleared by |
|---|---|---|---|
| `_SOLVED` | symbol + capture time | until a newer capture | `clear_caches` |
| `_HISTORY` | symbol, days, interval | 900 s daily / 15 s intraday | refresh, `clear_caches` |
| `_QUOTES` | symbol set | 5 s | refresh |
| `_LIVE_CHAINS` | symbol | 20 s, open session only | refresh |
| `_OI_CHAINS` / `_OI_EXPIRIES` | symbol (+ expiry) | 300 s / 900 s | refresh |
| `_EVENTS` | symbol | process life | `clear_caches` |

**After changing backend code or importing data, restart `optscan serve`.** Two servers
can share port 8000 (one on 127.0.0.1, one on 0.0.0.0) and localhost then reaches the
old build; check with `Get-NetTCPConnection -LocalPort 8000`.

---

## 7. Frontend (`frontend/src`, React + Vite; built to `frontend/dist`, served by FastAPI)

`App.jsx` holds two navigation levels: account views in the sidebar, per-symbol tabs on a
ticker page. `api.js` is the only HTTP client.

| view | where | calls |
|---|---|---|
| `Home` | sidebar | `home` |
| `Browse` | sidebar | `catalogue`, `pin`, `unpin` |
| `Ideas` | sidebar (Trade ideas) | `ideas` — shows the spread plan rows |
| `Backtest` | sidebar | `backtestRules`, `runBacktest` |
| `Positions` | sidebar | `positions` |
| `Journal` | sidebar | `journal`, `importStatement`, annotations, screenshots |
| `Underlying` | ticker tab Chart | `chain`, `history` |
| `Chain` | ticker tab Options chain | `chain` |
| `News` | ticker tab | `news` |
| `Information` | ticker tab | (summary from `symbol`) |
| `KeyLevels` | ticker tab Key levels | composes `Levels`, `Gex`, `Signals` |
| `Levels` | inside KeyLevels | `levels` |
| `Gex` | inside KeyLevels | `gex` |
| `Signals` | inside KeyLevels, and account-wide | `signals`, `recentSignals` |
| `BestPlays` | ticker tab | `scan` |
| `Payoff` | ticker tab | `payoff` |

Shared pieces: `components/common.jsx` (Panel, Stat, Note, Provenance, useAsync),
`components/chart/` (price chart, plot frame), `components/LevelsChart.jsx`,
`components/OiGexChart.jsx`, `components/PayoffChart.jsx`, `components/VolCharts.jsx`.

---

## 8. Module index (every module, one line; enforced by `tests/test_map.py`)

**Top level** — `__main__.py` (python -m optscan) · `cli.py` (command entry) ·
`config.py` (settings; the only reader of env vars) · `console.py` (terminal colour) ·
`logging.py` (structlog) · `market_calendar.py` (sessions, hours) · `tickers.py`
(ticker validation, path safety) · `universe.py` (groups from universe.yaml) ·
`catalogue.py` (what is known about every symbol) · `alerts.py` (alert sinks,
once-per-condition delivery)

**models/** — `base.py` · `enums.py` · `market.py` (Quote, OptionContract, OptionChain,
PriceBar, LiveQuote) · `snapshot.py` (ChainSnapshot) · `opportunity.py` (scored
candidate) · `position.py` · `events.py` · `broker.py` · `vendor.py` (VendorDailyBar)

**providers/** — `base.py` (the vendor contract) · `errors.py` · `parsing.py` ·
`ratelimit.py` · `retry.py` · `yfinance_provider.py` · `alpaca.py` ·
`robinhood_orders.py`

**storage/** — `db.py` (connection, migrations, watchlist, snapshot manifest) ·
`snapshots.py` (parquet chains) · `vendor.py` (vendor_daily, iv30_series,
preferred_source) · `vol_index.py` · `validation.py` · `positions.py` · `signals.py` ·
`ledger.py` · `journal_book.py` · `jobs.py` · `holdout.py` · `option_tape.py` ·
`conditions.py`

**imports/** — `robinhood.py` · `marketchameleon.py` · `fill_times.py`

**screener/** — `config.py` (screen.yaml) · `context.py` (analyze_snapshot: solve a
capture once) · `filters.py` · `scoring.py` (composite score) · `scan.py` · `gaps.py`
(mispricing hunt) · `history.py` (ATM IV history from captures) · `positions.py` ·
`strategies/base.py` · `strategies/singles.py` · `strategies/spreads.py`

**analytics/** (pure math, no I/O) — `greeks.py` (BSM, scalar) · `vector_bsm.py` (BSM,
numpy) · `iv.py` · `ivrank.py` · `surface.py` · `probability.py` · `moves.py` ·
`montecarlo.py` · `projection.py` · `liquidity.py` · `returns.py` · `payoff.py` ·
`events.py` · `levels.py` (levels, RSI, EMA, ATR) · `rules.py` (backtest entry rules,
`REGISTRY`) · `signals.py` · `triggers.py` · `portfolio.py` · `positions.py` ·
`ledger.py` · `journal.py` · `insights.py` · `outcomes.py` (settlement) ·
`calibration.py` (validation statistics) · `costs.py` (Corwin-Schultz) · `backtest.py`
(engine, nulls, option and spread modes) · `premium.py` / `premium_stats.py` (0–1 DTE
study) · `gex.py` (walls, max pain, GEX, zero gamma) · `bounce_spread.py` (the bounce's
call debit spread plan)

**jobs/** — `snapshot.py` · `load.py` · `scan.py` (incl. `load_iv_history`) ·
`prices.py` · `vol_indices.py` · `validate.py` · `signals.py` · `ideas.py` ·
`backtest.py` (incl. `load_series`, `load_implied`) · `search.py` · `option_tape.py` ·
`premium.py` · `conditions.py` · `manage.py` · `schedule.py` · `health.py` ·
`backup.py` · `launcher.py` · `powershell.py`

**live/** — `hub.py` (polling refresh loop + delta encoder)

**api/** — `app.py` · `deps.py` · `schemas.py` · `views.py` · `routers/backtest.py` ·
`routers/catalogue.py` · `routers/health.py` · `routers/ideas.py` ·
`routers/journal.py` · `routers/levels.py` · `routers/live.py` · `routers/payoff.py` ·
`routers/positions.py` · `routers/quotes.py` · `routers/refresh.py` ·
`routers/scan.py` · `routers/signals.py` · `routers/symbols.py` ·
`routers/watchlist.py`

**Outside `src/`** — `scripts/studies/` (pre-registered research studies + results),
`scripts/coverage_floor.py`, `screen.yaml` (screener thresholds), `universe.yaml`
(symbol groups), `DECISIONS.md` (why), `CLAUDE.md` (rules and current phase).

---

## 9. Lookups that have bitten before

- **A symbol's IV history:** `jobs/backtest.load_implied(settings, symbol)`. Never
  `iv30_series(..., source=preferred_source(...))` — `preferred_source` is the *price*
  source, which is Alpaca for most names, and Alpaca has no IV30.
- **IV rank on any surface:** go through `screener/context.analyze_snapshot(...,
  iv_history=load_iv_history(...))`. Skipping the history silently drops the component;
  the validation study logged nulls for six weeks that way.
- **A live chain for display:** `deps.live_chain` (open session only, 20 s). **A chain
  for open interest:** `deps.oi_chain` (any time, 300 s). Neither ever writes storage.
- **What can be backtested:** underlying rules on 294 symbols; single options and
  spreads at 15–60 DTE on the 24 IV30 symbols (`TENOR_BAND`); 0–1 DTE on QQQ/SPY from
  the option tape. **Not** gamma, walls or anything needing historical per-strike open
  interest, and nothing at 7 or 90 DTE.
