# MAP: how the app is wired

Read this before tracing anything by grep. It is the index; the facts live in
`map/generated/` and the meaning in `map/notes.md`. Open only the file your question
needs.

**Keep it current.** The facts are generated: after changing a module, table, route,
view, job, cache or command, run `optscan map generate` and commit the result with the
change. `tests/test_map.py` fails when the generated files differ from the code, when a
table or job has no note, when a note names something that no longer exists, when a
note's item has been rewired since it was last read (`optscan map stamp <key>` after
re-reading it), and when this file or the notes outgrow their size budget. `optscan map
check` runs the same checks in a second. Present tense only; history goes to
`DECISIONS.md`.

| question | file |
|---|---|
| What does each module do? | `map/generated/modules.md` |
| What writes and reads each table? | `map/generated/storage.md` |
| What feeds each route, who calls it, and what is cached? | `map/generated/routes.md` |
| Which dashboard file calls which route? | `map/generated/views.md` |
| What runs unattended, and when? | `map/generated/schedule.md` |
| Which modules does each command use? | `map/generated/commands.md` |
| Why does a table or job exist? What has bitten before? | `map/notes.md` |

---

## The whole thing on one screen

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

**analytics/ and screener/ never do I/O.** Vendors are reached only through
`providers/`, storage only through `storage/`, and both are orchestrated by `jobs/`
(batch) or `api/deps.py` (requests).

## Vendors

| vendor | gives | through |
|---|---|---|
| yfinance | option chains (bid/ask, OI, IV), expiries, quotes, events | `providers/yfinance_provider.py` |
| Alpaca (free) | daily and minute stock bars, delayed live quotes, historical option **trades** (no quotes) | `providers/alpaca.py` |
| Market Chameleon | daily CSV: IV30, call/put volume and OI **totals**, 2014 on (downloaded by hand) | `imports/marketchameleon.py` |
| Cboe indices | VIX, VXN, GVZ closes via Yahoo | `jobs/vol_indices.py` |
| Robinhood | activity CSV; order history for fill times | `imports/robinhood.py`, `imports/fill_times.py`, `providers/robinhood_orders.py` |

**No vendor here has historical per-strike open interest or historical option quotes.**
That decides what can be backtested (`trap:what-can-be-backtested` in the notes).

## Files under `data/`

| path | written by | read by |
|---|---|---|
| `snapshots/symbol=X/session_date=D/*.parquet` | `jobs/snapshot.py` via `storage/snapshots.py` | `jobs/load.py`, `screener/history.py`, `api/deps.py` |
| `option_tape/underlying=X/session=D/` | `jobs/option_tape.py` via `storage/option_tape.py` | `jobs/premium.py` |
| `optscan.sqlite` | `storage/` | `storage/` (tables in `map/generated/storage.md`) |
| `vendor/marketchameleon/` | no code: placed by hand | no code |
| `alerts.jsonl` | `alerts.py` | the user |
| `logs/` | `logging.py` | the user |

## Known gaps

- `jobs/conditions.py` is not a scheduled job, so the forward gamma record grows only
  when someone runs `optscan conditions`.
