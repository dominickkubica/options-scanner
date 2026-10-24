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
| 2026-10-22 | call debit spread plans on oversold bounce ideas; study harness; `scripts/check.ps1`; generated wiring map (`optscan map`, `map/`), trap tests, CI workflow, `docs/` + `scripts/build_doc.py`; debit positions with planned exits, `optscan forward-test`, `conditions` job, suite on pre-push |

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
