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
