# Architecture Decision Log

Append only. Newest entry at the bottom. One entry per session, written at the end
of the session. Record what was decided and why, not what was typed.

---

## 2026-07-30: Phase 0 scaffold

**Python 3.12, not 3.13.** The machine defaults to 3.13, but py_vollib and its
scipy/numpy pin are safest on 3.12 and the roadmap specifies it. The venv is pinned
via `py -3.12`. `requires-python = ">=3.12,<3.13"` makes an accidental 3.13 install fail loudly.

**src layout with a hatchling build.** Keeps `tests/` importing the installed package
rather than a directory that happens to be on sys.path, which is what makes the
"never import yfinance outside providers/" rule testable.

**Vendor import ban is enforced by the linter, not by discipline.** ruff TID251 bans
`yfinance` project wide with a per-file exception for `src/optscan/providers/*`. Add
each new vendor SDK to that banned-api list when it is introduced.

**Config is a single pydantic-settings class and the only reader of os.environ.**
Env prefix `OPTSCAN_`. Secrets are `SecretStr` and `safe_summary()` reports only which
credentials are set, never their values, so startup logs are safe to paste into a bug report.

**Logging is structlog, console in dev and JSON in prod.** Timestamps are ISO UTC.
This matters more than it looks: Phase 1 starts a daily job whose only observable
output for weeks is its logs.

**Dependencies are added per phase, not up front.** Phase 0 installs pydantic,
pydantic-settings, structlog, and the dev tools. yfinance, duckdb, pandas, py_vollib,
and fastapi arrive in the phase that first uses them, so a broken transitive pin
blocks the phase that needs it rather than the scaffold.

**pre-commit runs pytest through `cmd /c`.** A bare relative entry like
`venv/Scripts/python.exe -m pytest` is resolved by Windows against the system directory,
so the venv python starts with `sys.prefix = C:\WINDOWS\SYSTEM32\venv` and cannot find
pytest. Wrapping the entry in `cmd /c` resolves it against the repo root instead. Worth
remembering before adding any other local hook that invokes the venv.

**Deferred, deliberately:** no typer yet (argparse covers a single boot command; revisit
when Phase 3 adds `optscan scan`), no mypy yet (pydantic carries the runtime contracts
for now), no CI (local pre-commit is the gate on a single-developer local-first tool).

**Open question for Phase 1:** the daily snapshot job needs a decided capture time and
a decided behavior on half days and holidays. `pandas_market_calendars` answers the
calendar question. The capture time is a judgment call: near close gives the most
useful IV mark but risks the job running while the feed is still settling.

---

## 2026-07-30: Phase 1 data layer

**Capture time is 15:45 market local, and that is configurable.** Answering the Phase 0
question. Late enough to be a real mark, early enough to miss the closing auction where
quotes widen. What actually matters for IV rank is not the exact minute but that every
day is sampled at the same point in the session, so the value is fixed in config rather
than derived from when the process happens to start.

**Windows Task Scheduler, not APScheduler.** An in process scheduler needs a process
that is always up, and this runs on a laptop that sleeps and reboots. Task Scheduler
starts the job late rather than not at all. The cost is that the schedule lives outside
the repo, which is why `optscan status` and the `snapshot_run` table exist.

**The scheduled time is converted from market local to machine local.** This machine is
in America/Los_Angeles, so 15:45 New York is 12:45 here. Registering the task at 15:45
machine time would have captured 45 minutes after the close, every day, silently. The
conversion is fixed at install time, so the two DST changeovers shift the real capture
by an hour until the task is reinstalled. Acceptable, and noted here so it is not a
surprise in November.

**A capture before the open is refused, not fudged.** `session_date_for` returns None
before the bell, because a pre open capture records yesterday's close under today's
session date. That is the kind of error that is invisible for a year and then quietly
poisons every IV percentile. `--force` exists for testing and labels its output honestly.

**Partial captures are written and flagged.** Losing eleven good expiries because the
twelfth timed out is worse than storing eleven with `partial=True`. Failures are also
recorded as rows in `snapshot_run`, because a gap in the history has to be explainable
later and an absent row cannot distinguish a crash from a holiday.

**Every record carries `source` as well as `fetched_at`.** Beyond the roadmap's
requirement. An IV history that silently mixes yfinance and Schwab marks is corrupt in a
way that is nearly impossible to detect after the fact, and Phase 5 makes that switch.

**Parquet partitioned by symbol and session date, with the partition values also written
as columns.** Mild duplication that makes any single file self describing and avoids
depending on hive partition inference when reading a tree.

**Reads force duckdb to UTC.** duckdb renders timestamptz in the session timezone, which
defaults to the machine's. Same instant, different wall clock reading per machine, which
is exactly the confusion this tool cannot afford.

**Filenames collide safely.** Capture filenames have one second resolution, so a second
capture inside the same second suffixes rather than overwrites.

**Dependency notes:** yfinance 1.5 and pandas 3.0 are what installed. The adapter is
written against `fast_info`, `options`, `option_chain`, and `history`, and classifies
failures by exception text because the vendor has no error taxonomy. That text matching
is the most fragile thing in the codebase and it lives in one file on purpose.

**Config gotcha worth remembering:** pydantic-settings JSON decodes complex types from
env vars before validators run, so `OPTSCAN_DEFAULT_WATCHLIST=SPY,QQQ` needed the
`NoDecode` annotation. Blank credential entries are also coerced to None, since
`.env.example` ships them empty and they otherwise report as configured.

**Open question for Phase 2:** what counts as enough IV history to publish a rank. Sixty
trading days is the usual answer, but a percentile over sixty days of a single regime is
confidently wrong rather than unavailable. The `confidence` field in the roadmap needs a
defined rule, not a vibe.

---

## 2026-07-30: Phase 2 analytics core

**Our own greeks, with vollib as a test oracle rather than the implementation.** The
roadmap said use py_vollib. py_vollib is deprecated in favour of `vollib`, and more to
the point, using it as an independent second opinion in the tests is stronger than using
it as the implementation: our math is readable, carries its own dividend handling, and is
checked against an outside implementation across a grid of inputs. It agrees to 1e-9.

**Greek units are trader units, stated in the module docstring.** delta per 1.00 of
underlying, gamma per 1.00, theta per calendar day, vega per volatility point, rho per
rate point. Mixing raw partials with these is how a number ends up 100 or 365 times
wrong, and the docstring exists so nobody has to guess which convention is in force.

**Time is ACT/365 to the 16:00 New York close, with intraday precision.** A 0DTE at
15:45 has fifteen minutes of life, not zero and not one day, and both roundings are
wrong in opposite directions.

**American exercise is documented, not modelled.** BSM is European. Calls on non payers
agree exactly, puts and dividend paying calls are understated, and the error grows with
moneyness. Stated in greeks.py rather than papered over, and assignment risk around ex
dividend is handled as an event flag instead.

**The vol solver refuses in five distinct named ways.** Crossed, one sided, too wide,
below intrinsic, above maximum, outside the bracket, and not identifiable. The last one
was added after the round trip tests failed: for a contract worth 1e-14 with a vega of
1e-13, Brent happily returns a root and that root is an artifact of where the search
landed. Publishing it would put a fabricated number in the IV history, which is worse
than a gap. The identifiability test is a vega floor.

**What makes a vol identifiable is time value, not price.** A deep in the money put
worth 7.99 against 8.00 of forward intrinsic carries as little volatility information as
a far out of the money call worth a hundredth of a cent, and by put call parity it has
the same near zero vega. Measured against the no arbitrage floor, not against spot
intrinsic: for an in the money call the gap between those two is carry on the strike,
not time value.

**Answering the Phase 1 open question on IV rank confidence.** Under 20 observations,
nothing is published at all: not a low confidence number, no number. 20 to 60 is LOW,
60 to 180 MEDIUM, 180 or more HIGH, and every level drops one step when completeness
falls below 80 percent of the trading days the window spans. The completeness rule
matters because the days the snapshot job fails are not random days, they are the days
the vendor was struggling, which correlates with the days the market was moving.

**Both IV rank and IV percentile are reported, because they disagree usefully.** Rank
normalizes against the range and so is destroyed by a single spike; percentile counts
days and is blind to how far the extremes are. A high percentile with a low rank means
vol is near the top of where it usually sits but far from its outlier high.

**The expected move conversion was wrong and is now exact.** First implementation used
the widely quoted "straddle times 0.85" and compared the result against spot*IV*sqrt(t).
Running it against the real SPY chain showed a 29.5 percent disagreement on a chain
whose ATM vol had been solved from those very options, which should have been near zero.
The cause: an ATM straddle is the mean absolute move, which is sqrt(2/pi) = 0.798 of one
standard deviation, so multiplying by 0.85 lands at 0.68 sigma and the comparison
measured a constant offset rather than the market. The straddle is now inverted exactly,
by solving the ATM straddle formula for sigma*sqrt(t) rather than using any multiplier,
which is also correct at high total volatility where the first order identity drifts by
over a percent. On the real chain the disagreement went to minus 5.4 percent and minus
2.0 percent, with the sign the skew predicts.

**Probability of touch is the first passage formula, not double the finish probability.**
The doubling shortcut is exact only at zero log drift. Validated against a Monte Carlo
simulation: the closed form sits just above the simulated frequency, which is the
expected direction since discrete monitoring misses crossings that reverse between steps.

**POP is measured at the breakeven, not at the strike.** A short put assigned a cent in
the money still keeps almost all the credit. Measuring at the strike understates POP most
where the credit is largest, which is where it matters.

**Delta is exposed as a proxy and labelled as biased.** Delta is N(d1), the ITM
probability is N(d2), and d1 exceeds d2 by sigma*sqrt(t), so delta always overstates.
About 8 points at a year and 20 vol. Shown next to the real number so the gap is visible.

**The margin model is written down in returns.py.** CSP is full cash less credit, not
reg-T, which would turn a 2 percent return into 10 on the same trade. Verticals and
condors are max loss. Naked calls deliberately return None, because unbounded loss has no
honest denominator. Annualizing is simple scaling, not compounding: compounding a 45 day
trade assumes you can find eight more like it.

**Liquidity drops missing components rather than scoring them zero.** Absent volume is
not illiquidity, and treating it as such would systematically punish whichever fields the
current vendor leaves empty, which changes when the provider changes in Phase 5.

**P50 is Monte Carlo and carries a standard error.** No closed form exists. Deterministic
given a seed, and every estimate reports its own uncertainty because an unqualified
percentage from a few hundred paths is overconfident. It re-prices at constant vol, which
understates P50 for a position sold into elevated IV, and understating is the safe side.

**Open question for Phase 3:** the gaps module needs a rule for what counts as a skew
anomaly rather than a normal smile. That needs a fitted smile and a residual threshold,
and the threshold should be calibrated against the snapshot history rather than picked,
which means it may have to wait until there is enough history to calibrate against.

---

## 2026-07-30: Phase 3 screener and scoring

**Generation is generous, filtering is strict, and every rejection is counted.** A
candidate that was never generated cannot be explained later, and an empty results
table with no explanation is the fastest way for a screener to lose its user. The
tally prints under every scan: "20236 candidates considered, 883 passed. Top
rejections: dte_too_long 7151, dte_too_short 5371, delta_too_low 3285."

**Every threshold is in screen.yaml and unknown keys are an error.** A typo silently
leaving a filter at its default would let a user believe a threshold is in force when
it is not, which is worse than a crash.

**Scan reads the last stored snapshot by default, not live quotes.** Fast, offline,
reproducible, and it makes the whole ranking pipeline testable against a frozen
fixture. `--live` fetches fresh. Either way the quote age comes back on the result and
anything over six hours is called out, because a scan of yesterday's close is a
legitimate thing to want and an illegitimate thing to mistake for live.

**Missing score components renormalize rather than scoring zero.** IV rank is the case
that matters: before the history is deep enough every symbol would score zero on it,
which would not change the ranking but would compress every score toward the bottom
and make an unavailable input look like a failed one.

**Liquidity takes the worst leg, not the average.** A position is only as fillable as
its hardest leg, and averaging lets a liquid short strike hide an untradeable wing.

**Strangles report no capital rather than a plausible one.** Undefined risk has no
honest denominator, so `max_loss`, `capital`, and both return figures come back None,
and the strategy ranks below anything with a real return. That is the right default
for a tool that cannot see your account.

### The gaps module, and three versions of the same mistake

The first implementation flagged 5139 mispricings on one SPY chain. The second flagged
2583. The third flagged 1646. Each round was the same error in a new place: **measuring
a constant offset and reading it as signal.** Worth recording in full, because it is
the third time this project has made that mistake and the pattern is now recognisable.

**Vertical mispricing.** Compared credit-over-width against the delta implied
probability and flagged the excess. That excess is the variance risk premium, which is
the entire reason premium selling exists, so it is positive nearly everywhere: 63
percent of a liquid chain cleared the threshold. Measuring each vertical against the
others in its own expiry and width did not fix it either, because the excess is not a
distribution with outliers, it is a smooth curve running from +0.002 far out of the
money to +0.268 at the money. Every near the money spread then reads as 20 or 40
deviations out, which is a statement about gamma. **This detector is now off by
default.** The measurement is right and the baseline is missing, and the baseline needs
history. Left in the code rather than deleted, for Phase 8.

**Put call parity.** Measured against `spot * exp(rt)` with a zero dividend yield and
flagged 1043 strikes. Every one was the same offset: the implied forward was 742.0
across every strike while the model forward was 742.45, and that 0.45 was SPY's
dividend yield over 22 days. Parity was holding perfectly. **Now solved from the chain
itself:** rearranging C - P = (F - K)exp(-rt) at the strikes nearest spot gives the
forward, and every carry assumption disappears with it. Checking only within 2 percent
of that forward, with both legs quoted inside 5 percent, took it to 56 across six
symbols. The strikes that survived the first two fixes but not the third were deep in
the money puts carrying real early exercise value, which breaks European parity
legitimately and in a consistent direction.

**Skew.** Fitted a quadratic across the whole listed strike range, where a quadratic is
a poor description of a smile, so every wing strike looked anomalous. Now fitted within
20 percent moneyness and flagged on a robust z score against the fit's own residual
scale as well as an absolute floor, because three vol points off the smile is
remarkable on a 12 vol index and unremarkable on a 90 vol single name.

**The lesson, written down so it gets applied first next time:** before believing any
detector, run it against the fixture and count the hits. A detector that fires on a
large fraction of a liquid chain is describing the market, not finding anomalies in it.

**Gap thresholds remain uncalibrated.** They are chosen to produce a reviewable list,
not because anything says they are right. That is the same answer as Phase 2's open
question, and it still needs history.

---

## 2026-07-30: closing out the Phase 3 loose ends

Both open questions from the Phase 3 entry are now fixed, and both turned out to be
the same constant offset mistake for the fourth and fifth time.

**Term structure comparisons now start a week out.** The backwardation flag fired on
five of six watchlist symbols. The cause was the front of the curve: as time to expiry
goes to zero the diffusive part of a move shrinks with sqrt(t) while the jump part does
not, so very short dated ATM vol is mechanically elevated. Real SPY numbers on the day:
0DTE at 26.9 percent, 4 days at 11.4, 22 days at 14.1, with nothing happening.
Comparing a back month against that reports every index as permanently inverted.

`TermStructure.slope` and `is_backwardated` now take a `min_dte` defaulting to 7 and
ignore anything nearer. The flag went from five of six symbols to two, AAPL and MSFT,
which were the two with earnings that week. `slope` became a method rather than a
property in the process, which is a small API break in Phase 2 code and was worth it.

**Commissions are modelled.** 0.65 per contract per leg by default, charged both
opening and closing, all configurable under `costs`. This is not a rounding detail: a
fixed fee is an eighth of a one point wide spread's maximum profit and noise against a
cash secured put's, so leaving it out does not shift candidates equally, it
systematically promotes the narrow ones. `ReturnProfile` keeps `gross_max_profit` so
the cut stays inspectable rather than merely applied.

**A minimum absolute profit, because a percentage does not say whether a trade is worth
doing.** A one point wide spread taking 0.21 annualizes at over 400 percent and clears
18 dollars after commissions. `min_max_profit` defaults to 25 dollars net. The scan
table now prints profit and capital in dollars next to the percentage, so a spectacular
return on a tiny base is visible rather than inferred.

Combined effect on the live watchlist: the top rows moved from one point wide spreads
paying 21 dollars to five and ten point spreads paying 98 to 213 dollars on 390 to 800
dollars of capital, and the 454 percent headline numbers went with them.

**Also done:** removed the empty `screener/rules/` package left over from the Phase 0
scaffold, since the filters and config ended up one level up. Coverage went from 83 to
91 percent, with `jobs/load.py`, `jobs/scan.py`, and the scan CLI now tested, and the
disabled vertical gap detector tested in its disabled state and its enabled one.

**Still open, and genuinely blocked rather than deferred:** every gap threshold, and
the vertical mispricing baseline. Both need snapshot history to calibrate against, and
the history is one day old. Phase 8 is where that gets settled.

---

## 2026-07-30: Phase 4 started, paused partway

Committed as work in progress rather than left in the working tree. See the Current
phase section of CLAUDE.md for the ordered list of what remains.

**Payoff curves are computed server side, not in the browser.** The expiry curve is
piecewise linear and a browser could draw it, but the T+0 curve needs Black-Scholes
repricing at every point, and putting a second pricing implementation in JavaScript is
how the two quietly disagree. One implementation, tested, in Python.

**Breakevens are solved, not sampled.** The expiry payoff has kinks only at strikes,
so every zero crossing lies on a straight segment between two of them and one linear
interpolation is exact. Scanning a grid would miss a crossing between samples and
report the rest slightly wrong.

**Only the upside is unbounded.** A stock cannot fall below zero, so a short put has a
real maximum loss, large and rarely quoted, while a short call has none. The first
implementation treated both tails symmetrically and reported no maximum loss for a
cash secured put, which is wrong and would have shown as an empty cell in the UI.

**API schemas are separate from the domain models.** The domain models are strict,
frozen, and carry things a browser has no use for. The wire formats are shaped for a
table or a chart and are allowed to flatten and rename. Keeping them apart means a UI
change never pulls on the models the analytics depend on. Two rules carry over and
matter more here: every payload with market data in it also carries when that data was
fetched, and a missing number serializes as null rather than zero, because a chart
that draws zero for "unknown" is lying where nobody can see it.

**Open question for the rest of Phase 4:** whether the dashboard should offer a live
refresh button that re-fetches from the provider, or stay strictly on stored snapshots
until Phase 5 brings a real feed. Stored only is the more honest default and makes the
whole UI reproducible; a refresh button invites the user to treat delayed yfinance
quotes as live.

---

## 2026-07-30: Phase 4 finished, the dashboard

The rest of what the previous entry left: the dependency layer, eight endpoints, the
app, and the whole frontend. Answering that entry's open question first, because
everything else follows from it.

**The dashboard shows stored snapshots and has no live refresh button.** That was the
open question and stored-only wins. A refresh button invites the user to treat delayed
yfinance quotes as live, and there is no label that survives contact with a chart: put
a number in a dashboard and it reads as current no matter what the caption says. The
whole UI is therefore reproducible from what is on disk, the capture time and its age
sit in the header on every screen, and anything over six hours is badged stale. Live
data is Phase 5's job, with a provider that actually quotes in real time.

**Two exceptions, both reference data rather than quotes, both stated when they fail.**
The corporate calendar is fetched because without it the API's screen would silently
differ from the CLI's: `exclude_earnings` would have nothing to exclude on, and the
dashboard would return candidates the same config rejects in a terminal, invisibly.
Daily candles are fetched because nothing stores them and a price chart is in the
phase's exit criteria. Both are cached, and both come back as an empty result carrying
the reason rather than as a blank panel. `ScanOut.events_checked` is false when the
calendar could not be reached, and the UI says so above the table.

**The solved symbol cache is keyed on the snapshot's own `fetched_at`.** A new capture
therefore invalidates it without anyone remembering to. Keyed on the symbol alone it
would serve yesterday's chain until someone restarted the server, which is the same
class of error as everything else this project keeps catching.

**And the cache needed a lock, which is the part that was nearly missed.** Opening a
symbol fires the summary, the chain, and the scan at the same instant, uvicorn runs
sync endpoints in a thread pool, and all three miss a cache none of them has finished
filling. The first version cached correctly and saved nothing on the one page load
where it mattered: the server log showed the same AAPL capture solved three times for
two requests. A per key lock with a re-check inside turns that into one solve and two
waits. Measured on the real six symbol data directory: four concurrent requests for SPY
produced four solves before, one after. The test asserts the count rather than the
timing, and was checked against the unlocked version to make sure it was not vacuous.

**Analysis is solved as of the capture, not as of now.** `analyze_snapshot` is called
without a `now`, so liquidity scores trade recency against the capture instant. A
snapshot from Friday then scores the same on Monday as it did on Friday, which is both
the honest reading for stored data and the thing that makes the result cacheable at
all. Quote age is computed fresh per response from `fetched_at` instead.

**The wire format never sends a zero it does not mean.** A contract with no solvable
implied vol serializes `iv: null` plus the solver's own named refusal, and the grid
prints that refusal in the cell. Every strike keeps its row even when nothing about it
could be computed, because dropping the row leaves a hole in the ladder that reads as
a strike which is not listed. The frontend has one formatting rule to match: `n/a` for
null and never a `|| 0` fallback anywhere.

**The chain grid's two column orders are written out in full.** The first version built
the put side by reversing the call side's cells, which also reverses bid past ask: a
normal 0.00 by 0.01 put rendered as bid 0.01 ask 0.00, a crossed market that was not
there. Two explicit orders, no cleverness.

**Payoff requests carry no prices.** The browser says which strikes, which rights, and
which direction; the server prices the legs from the same solved snapshot the rest of
the page came from. A client that could post its own prices could post a payoff for a
position nobody could enter, and it would look exactly as convincing as a real one.

**Default expiry is the server's choice, not the front of the chain.** The first
screenable expiry, meaning the nearest one at or beyond the screen's own `min_dte`.
Opening on the front expiry lands on a zero or one day chain whose vol is the least
representative on the board and whose grid is 25 percent solved. The browser asks with
no expiry and reads back which one it got, so the threshold stays in config where it
belongs.

**Two charts needed a band, for the same reason the analytics did.** The skew curve
plots strikes within 20 percent of spot, the same band the smile fit uses in the gaps
module. Without it a far out of the money strike quoted 0.00 by 0.05 solves to a real
195 percent implied vol and compresses a 14 vol smile into the bottom two pixels. The
chain grid defaults to 25 percent either side for the same reason and always prints how
many strikes are hidden. Neither is cosmetic: an axis scaled by an artifact is a chart
about the artifact.

**Charts are hand rolled SVG except the candles.** A payoff diagram needs zero to be a
real axis rather than the bottom of the plot, and a null point has to break the line
rather than bridge it, which is the drawing equivalent of rendering unknown as zero.
Candles plus a volume histogram on its own scale is the one shape worth a dependency,
so that one uses lightweight-charts.

**Still open, and still blocked on history:** every gap threshold and the vertical
mispricing baseline, unchanged from Phase 3. The dashboard makes the shortage visible
rather than fixing it, which is the correct thing for it to do: every IV rank on the
watchlist currently reads `insufficient` with "Only 1 observations, need 20" under it.

**Open question for Phase 5:** the API is stateless per request and the frontend polls
nothing. When a real time provider arrives, the choice is between pushing over
websockets and letting the client poll a cheap endpoint. Pushing is the better
experience and the harder thing to keep honest, because a partially updated screen
where the chain is live and the scan is four minutes old is worse than one that is
uniformly four minutes old and says so.

## 2026-07-31: Phase 5, live data and the Tradier adapter

**The vendor documentation was read, not remembered.** Everything the adapter depends
on came from docs.tradier.com on 2026-07-31, at these URLs. The site has moved since
the roadmap was written: documentation.tradier.com now 308s to docs.tradier.com.

- `/reference/brokerage-api-markets-get-quotes`, `-get-options-chains`,
  `-get-options-expirations`, `-get-history`, `-get-clock`
- `/reference/brokerage-api-streaming-create-market-session`,
  `/reference/websocket-market-data-streaming`
- `/docs/endpoints`, `/docs/rate-limiting`, `/docs/faq`

Verified from those pages: hosts are `https://api.tradier.com` and
`https://sandbox.tradier.com`; auth is `Authorization: Bearer <token>` with
`Accept: application/json`; market data is limited to 120 requests a minute in
production and 60 in sandbox, per access token per minute, with the running count in
`X-Ratelimit-Allowed`, `-Used`, `-Available` and `-Expiry`. Two shapes are worth
naming because they are the ones that bite: `quotes.quote` is a `oneOf`, an object for
one symbol and an array for several, and the epoch fields carry no documented unit, so
the adapter infers seconds or milliseconds by magnitude rather than assuming.

**Two facts from that reading reshaped the phase.** Their FAQ states that sandbox data
is delayed by fifteen minutes, and that "Presently, we do not offer a delayed streaming
endpoint for paper trading." The free tier this project is built against therefore has
no push at all. The websocket exists, it needs a production session, and it would still
be delayed here.

**So the open question from Phase 4 is answered: server sent events, and the server
polls.** There is no vendor stream to forward, only a refresh loop to announce. SSE
because the traffic is one directional, because EventSource reconnects with backoff on
its own where a websocket would have that written by hand, and because it needs no new
dependency. Subscription changes go the other way as ordinary HTTP, since they happen
when somebody clicks a symbol rather than sixty times a minute.

**The honesty worry about deltas is answered by choosing the right unit.** The Phase 4
note said a screen where the chain is live and the scan is four minutes old is worse
than one uniformly four minutes old. So the unit of update is a whole cycle: one quote,
one chain, one solve, one `fetched_at` over all of it. A delta then names only the
contracts whose numbers moved, which is not a mixture of ages, because a contract that
did not move holds the same value at this version as at the last. Two rules make that
true and both are enforced: a fetch that fails or comes back partial emits a status
event and never becomes a version, and a contract that leaves the chain is named in
`removed` rather than left to sit at its last price forever. A subscriber that falls
behind has its queue cleared and its next cycle sent in full, because a delta applied
against a version the client never received is silent corruption rather than lag.

**The grid shows live or stored, never a blend.** Not an overlay. Overlaying would put
a live bid beside a stored delta in one row under one timestamp, which is the exact
screen this phase set out not to build. The two sources have different strike sets and
different ages, so the table renders one of them and its header says which.

**Live cycles are solved the same way stored snapshots are**, through `analyze_chain`
with our own rate. Tradier ships ORATS greeks and implied vols; those are kept as
`vendor_iv` for comparison only, exactly as yfinance's are. Switching provider must not
move a number that is ours.

**The IV history no longer pools vendors, and this was a live bug rather than a
precaution.** `atm_iv_history` grouped every stored session regardless of the `source`
column that Phase 1 put there for this purpose. Switching to Tradier would have mixed
its marks into a yfinance series with nothing downstream able to see it. The source is
now a required argument, sessions from other vendors are excluded and counted, and the
count is reported next to the rank so that a confidence level dropping after a switch
does not read as a failing job. The live feed never writes to storage at all: the daily
snapshot job stays the single writer of the history, sampled deliberately at 15:45
rather than at whatever moment a browser happened to be open.

**`realtime` became a function of the settings rather than a set of provider names.**
For Tradier it is not a property of the vendor: sandbox is delayed no matter what, and
production depends on a market data entitlement that no response announces. So
`tradier_realtime_entitled` is asked and defaults to false, sandbox overrides it to
false regardless, and `/api/health` reports `delay_minutes` alongside. Null there means
the delay is unknown, not zero: yfinance is delayed but publishes no number, and
reporting an inferred one beside a documented one would give them equal weight.

**Nothing is polled while the market is closed**, and polling slows by a configurable
multiple outside the regular session. Every fetch would return the same settled
numbers, and the budget spent confirming that is budget missing on Monday morning. The
session comes from the existing offline market calendar rather than Tradier's clock
endpoint, which keeps it free and testable.

**The rate limiter is separate from the retry helper** because they solve opposite
problems: retry reacts after a call has failed, and a limiter stops the call being
made. Against a per minute budget a pure 429 handler would burn the very thing it
protects. The vendor's own counter wins over the local bucket when it is lower, and
never when it is higher, because a vendor count above ours usually means their window
is about to roll.

**Tradier has no corporate calendar**, so `get_events` stays unimplemented and inherits
the base class refusal. Phase 4 already handles that: the screen widens and says it
could not check earnings. Switching provider therefore weakens the earnings exclusion,
visibly, which is the correct behaviour and worth knowing before switching.

**The ban list got httpx rather than a vendor package.** Tradier publishes no SDK, so
the thing a shortcut from a router would have to import is the HTTP client itself. Same
rule as yfinance, one layer down.

### What is verified and what is not

Verified in a browser against a live yfinance feed during the 2026-07-31 session, with
the market open: the stream connects, cycles arrive on the poll interval without a
refresh, the header shows connection and session state, the grid switches to live and
says so, and the age next to it climbs between cycles.

**Not verified: anything against Tradier itself.** No token existed when this was
written. `tests/fixtures/tradier/` is built from the response schemas above and its
README says so; those files pin the parsing, the one-or-many quirk and the failure
mapping, and they are not evidence that the live API behaves this way. The first thing
to do with a real sandbox token is recapture them.

**A defect this found in its own design, worth recording.** The hub first resolved an
unnamed expiry to the front month while the chain endpoint resolves it to the first at
or beyond `min_dte`. The symptom was the nastiest kind available: the stream connected,
cycles arrived, the header said live, and the grid never moved, because the two sides
were following different expiries. Both now use one rule and a test pins it.

**Also worth recording: the SSE endpoint cannot be integration tested with
TestClient.** Starlette's buffers a whole response and returns only once the app sends
its final body message, which a stream never does, so a request against it does not
read slowly, it never returns at all. The generator is exercised directly instead, and
the browser check covers the plumbing between it and a socket.

**Open for Phase 6 and later:** the live feed covers the chain grid only. The
opportunities table, the term structure and the skew curves still read the stored
snapshot, and they say so. Extending live to the scan means deciding what a scan of a
mid session chain even means when its IV rank comes from a history sampled at 15:45,
and that is a question about the signal rather than about the transport.

## 2026-07-31: Phase 6, levels and projections

**The seventh instance of the constant offset trap, and it was in the filter written to
prevent it.** `min_prominence_atr` requires a swing to be deeper than some multiple of
ATR before it counts as a pivot. That sounds like a significance test. It is not: the
prominence of a local extreme inside an eleven bar window is the depth of an eleven bar
range, and an eleven bar range is about one ATR by construction. So thresholding it in
ATR units thresholds a constant. Measured on the real capture: 123 pivots at 0.0 ATR,
still 123 at 0.5, 118 at 0.75. It defaults to zero now and its docstring records the
measurement so the next person to reach for it does not repeat it.

**The eighth was next to it, and is the more interesting one: a touch count is a
density.** Clustering pivots and keeping the ones touched twice or more publishes 31
levels on four years of SPY, and every one of them looks like evidence. It is not.
Scatter 123 pivots across a 390 point range and cluster in 4.5 point bands and about
1.45 land in each band by arithmetic alone, so "touched twice" is what chance produces
and "touched seven times" is the only kind of number worth looking at.

The fix is a baseline, and the baseline had to be the right one. A uniform null over the
price range would have been wrong in the familiar direction: price does not visit all
prices equally, it spends months in a congestion zone and crosses a gap in a day, so a
uniform null calls everything inside the congestion zone significant and everything
outside it noise. `occupancy_share` therefore measures how much time price actually
spent in each band, and the expected touch count is the pivot count times that share.
The test is a Poisson tail on the excess.

What it does to the numbers is the point of the whole module: 31 candidates, 8 at p
below 0.20, 1 at p below 0.05. On the 365 day window the API serves, 10 candidates and
zero survivors.

**Zero survivors is a finding, and the UI says so rather than showing an empty panel.**
"Nothing here turned out to be a level. Every candidate was a price where the number of
times price turned is what the time it spent there already predicts." An empty list with
no explanation would read as a broken job, and the honest reading is considerably more
interesting than a chart full of lines would have been.

**Two filters aimed at the same thing, one of them silently disabling the other.** Worth
recording as a pattern rather than as one bug. Raising the prominence default to 1.5 ATR
to make it "work" cut the pivot count to 66, which starved the significance test of the
counts it needs, which took the surviving level count from 1 to 0. A confounded filter
stacked in front of a principled one does not merely fail to help.

**The volume profile reproduces the trap in its purest form and is left visible.** Over
the full 1100 sessions the point of control lands at 413 against a spot of 748, because
that is where the 2022 to 2026 advance spent the most time. That is a fact about the
path, not a price buyers defend. `build_levels` trims the profile to 120 sessions and
says in a note that it did, and a test pins the long window behaviour so the bias stays
documented rather than becoming folklore.

**Round numbers carry a constant strength on purpose.** There is nothing to measure. A
round number has no touch count and no p-value, and giving it a computed looking score
would put it on the same footing as a level that earned one. They are drawn dashed and
faint, and their `p_value` is null rather than 1.0, because null means "not tested" and
1.0 would mean "tested and failed".

**The cone uses each expiry's own implied vol, not one vol over a sqrt(t) curve.** The
single vol version is what everyone draws and it is wrong for anyone selling more than
one expiry: the term structure slopes, and it inverts around events, so projecting the
front week out to ninety days draws a cone that is too narrow at the back in contango
and too wide when inverted. The error is largest exactly around the events a premium
seller most wants to see. The cone is a polyline through one point per expiry and the
straight lines between points are labelled as interpolation.

Bands are lognormal rather than symmetric. Over a week the two are indistinguishable;
a year out a symmetric band puts its lower edge at a price the model assigns almost no
probability to, which looks like precision and is an artifact.

**The variance risk premium is refused across mismatched tenors.** Comparing a 30 day
realized vol against the frozen fixture's 4 day implied is the term structure talking,
not the premium, so `_comparable_implied_vol` picks the expiry nearest the realized
window and returns None when nothing is within a factor of two. On live SPY it does
publish, and the first thing it showed was implied 12.1 against realized 12.8: a
negative premium, which the UI flags as unusual rather than rendering as a small
positive number the reader would skim past.

**One endpoint, not four.** The levels panel is the only screen that draws a number
fetched seconds ago on the same axes as one from the last stored capture. Splitting it
across four requests would let the browser assemble one picture out of four moments,
and this is precisely the panel where that matters, since the whole purpose is judging
a strike against a level. The payload carries two provenances and the header shows both.

**A failed candle fetch costs the levels and nothing else.** The cone and the strikes
come from the stored capture, so they still render. An honest partial beats an error
page, and a test pins it.

### What is verified

Full suite and ruff green. Verified in a browser against live data on 2026-07-31 with
the market in its post session: one chart carrying the close line, 11 levels, both cone
bands, 25 candidate strikes shaded by probability of profit, the two provenances, and
the terminal distribution histogram for the selected expiry. `optscan status` before and
after shows the snapshot job unaffected and today's capture present for all six symbols.

**A subtlety the chart now states.** A candidate strike's shade is the probability of
profit of the position the screener built around it, not of the strike alone, so two
neighbouring strikes can read 62 percent and 75 percent when one is a spread and the
other a single leg. That looked like a bug on the first render and is not, so the panel
explains it rather than leaving it to be rediscovered.

**Still blocked on history, unchanged:** every gap threshold and the vertical
mispricing baseline. Phase 6 does not touch them. IV rank is still `insufficient`
everywhere, now on two captured sessions rather than one.

## 2026-08-01: Phase 7, positions and risk

**The fill price is required and there is no default.** Every other number in this
project can be recomputed from a stored snapshot; the price a person actually got
cannot. A tool that opened a position at the mid and then reported profit against the
mid would show a trader flat at the moment they have already lost half the spread, and
on the wide contracts a premium seller lives in that is most of the last of the credit.
So `PositionLeg.fill_price` has no default, the CLI leg parser refuses a spec without
it, and the column is NOT NULL.

**Positions are not `Record`s.** Everything else in models/ came from a vendor and
carries `fetched_at` and `source` so its age can be judged. A position came from a
person. Forcing it into the same base would mean inventing a source for a fact that has
none, so there is a separate `UserRecord` base and the distinction is visible in the
type.

**One sign convention, both directions.** `signed_fill` and `signed_value` are positive
for a credit and negative for a debit, so profit is always `signed_fill - signed_value`:
a put sold at 2.00 now marked 1.00 gives 2.00 minus 1.00, and a call bought at 3.00 now
marked 5.00 gives -3.00 minus -5.00. One formula rather than a branch on direction,
which is what keeps the aggregate honest when a position holds legs of each kind.

**Held positions mark at the closing price, not the mid.** A short leg marks at the ask
because that is what buying it back costs; a long leg marks at the bid. Marking a short
book at the mid overstates every position by half the spread on every leg, and it does
so worst in exactly the range where a profit target fires. `mark_convention` is
configurable and defaults to closing.

**Greeks refuse; profit sums.** A four legged condor summed over the three legs that
solved reports a delta that is not the position's delta, and it is wrong in the
direction of whichever wing failed, so any missing leg withholds the whole greek.
Profit is different: an incomplete total is still the profit of the positions in it,
and `unmarked` says how many are missing. A profit total gets read; a delta gets used to
size a hedge, and that asymmetry is why they are treated differently.

**Beta is None rather than 1.0 when it cannot be estimated.** A default of 1.0 is a
measured looking number for an unmeasured thing, and it would quietly weight a thinly
traded name as though it moved exactly with the index. Below sixty overlapping sessions
`beta` returns a `Beta` whose value is None and whose caveat says why, and the portfolio
withholds its beta weighted delta rather than publishing a total built on a guess.

Three limitations are documented on the module rather than left to be discovered: beta
is an estimate that moves, it is a linear fit and correlations go to one in a crash
which is precisely when it is being relied on, and beta weighting an option position is
a first order estimate of a first order estimate. Returns are matched by session date
rather than by position, because two vendors can disagree about holidays and a one day
offset turns a beta of 1.0 into noise.

**Early assignment is decided by extrinsic value, not by moneyness.** Reused from
`analytics/events.py`, which got this right in Phase 2: a short call is at risk when the
dividend exceeds the remaining time value, because that is when exercising is rational.
Deep in the money and near an ex date is not sufficient, and giving up 3.00 of extrinsic
to collect 1.50 is not something anybody does. The trigger computes extrinsic as the
mark less the intrinsic rather than using the mark directly.

**Delta breach is measured per leg, not on the net.** An iron condor's net delta can sit
near zero while one side is badly tested, and the net would report the book balanced
right up until assignment.

**There is deliberately no stop loss trigger.** It is popular and it would have been
easy. On a short option it systematically closes the positions that were about to
recover, because the loss is largest when the move is largest and the move is what mean
reverts. Shipping it would need Phase 8 evidence, and without that it would be this
project asserting a rule it cannot defend. The omission is recorded in the module
docstring so the next person finds the reasoning rather than the gap.

**An alert fires once per condition, and the suppression is in the database.** In
memory it would reset on every restart. This is the difference between a tool that gets
trusted and one that gets muted, and a muted alerting tool is worse than none because it
is still believed to be working. The uniqueness is a database index rather than a read
then write, so two runs racing cannot both decide they are first.

**A delivery failure is not recorded as sent.** If no sink accepts an alert it is left
unrecorded and retried next run, because a webhook outage must not consume the only
notification a condition will ever send. A sink that raises is caught and the others are
still tried: the run matters more than any one notification.

**The default sink needs no account.** Telegram, Discord and SMTP all require somebody
to register somewhere, and a default that cannot run until they do is a feature that
does not work out of the box. So the default is a JSONL file next to the database plus a
structured log line. `WebhookSink` exists for the rest and is **untested against a real
endpoint**, which its docstring says: this project has no webhook to try.

**Position entry stays on the CLI and the API is read only.** A fill price typed into a
browser form is unverifiable and a mistyped one silently corrupts every number on the
page. More importantly, a GET that delivered alerts would fire them on every refresh,
so `/api/positions` runs the evaluation with `send_alerts=False` and only `optscan
manage` sends.

### What is verified

Full suite and ruff green. Verified end to end on 2026-08-01 by opening two paper
positions through the CLI, running `optscan manage`, and reading the result in the
browser:

- A put credit spread marked from the stored snapshot at +221.40, 71 percent of maximum
  profit, firing the profit target trigger.
- A deliberately tested short 800 put firing both `delta_breach` and `tested`, with two
  alerts delivered to the log and the JSONL file.
- A second run delivering zero alerts while still reporting both conditions, which is
  the once-only suppression working.
- Portfolio totals with a beta weighted delta of 62,304 dollars of SPY exposure, and
  the same totals withheld with a stated reason when run with `--offline`.
- Signs checked against expectation on a real put credit spread: positive delta,
  positive theta, negative vega.

Both paper positions were deleted afterwards, so the database is clean.

**Not verified: the webhook sink**, for the reason above, and **nothing here has been
validated against outcomes**. Every threshold in `ManagementConfig` is a convention
rather than a finding. That is what Phase 8 is for, and the alert JSONL file was chosen
partly because it is the record Phase 8 will want when it asks whether any of these
triggers were worth acting on.

## 2026-08-01: Phase 8, validation

**Phase 8 works without the historical backfill, and the reason is worth stating.** The
scoring needed an option chain. The settling does not: a short option at expiry is worth
max(strike - spot, 0) or max(spot - strike, 0), which is arithmetic rather than a quote.
So resolving a logged candidate needs one number, the underlying's close on expiry day,
which yfinance gives away. The backfill would let history be *scored* retroactively;
nothing here needs it to be *settled*.

**Every candidate is logged, not just the good ones.** `--limit` exists and warns when
used. A study that records only what the score already liked measures the trader rather
than the score, and would confirm whatever they believed going in.

**The score is denormalized onto the log row.** Weights will change, and a resolved
outcome has to stay attached to the score the candidate was actually given. Re-scoring
history under new weights is a legitimate and different question, and answering it must
not overwrite this one. The scan row also carries a digest of the effective config, so a
later reader can tell whether two runs were even comparable.

### The trap this phase brings, which is the ninth instance in a new costume

Every previous instance was a *measure* with a structural component read as signal.
This one is a **sample** with a structural component read as evidence.

The rows in the validation log are nowhere near independent. One scan of one chain
produces forty candidates that share an underlying, a session and a volatility surface,
so if it rallies every short call in that scan loses together. Consecutive sessions on
the same symbol and expiry are very nearly the same trade recorded twice. Strikes 700
and 705 on one chain resolve together almost always.

The first recording run logged **903 candidates from six symbols in one session**. Taken
at face value that is 903 observations; it is closer to a few dozen. An interval computed
on the row count is roughly the square root of the cluster size too narrow, which here is
about a factor of five, and that is more than enough to turn noise into a finding.

So the unit of independence is the **(symbol, expiry) cluster**, never the row. Win rates
are still reported per row because that is what a person wants to read, every interval is
widened to the cluster count, and both numbers are printed. Below twenty clusters no
conclusion is drawn at all.

**Wilson intervals rather than normal.** Short premium lives near a win rate of 0.9,
where the textbook normal interval produces upper bounds above 1.0. An upper bound above
certainty is a visible sign of a formula being used outside its range, and the ones that
are not visible are the problem.

**Calibration is measured on the event, not on profit.** The model predicts where price
finishes; whether that was survivable is a separate question. A position can be breached
and still make money, and scoring on profit would let a model be wrong about the event
and still look calibrated. `probability.py` already states the expected direction of
error, so finding the far strikes breached more often than predicted is the anticipated
result rather than a discovery.

**Brier score alongside the reliability table**, because a table can look fine while the
forecasts carry no information: predicting 0.8 for everything is perfectly calibrated on
average and completely useless, and the Brier score notices while the table does not.

**Hold to expiry is a counterfactual and is labelled as one on every report.** Nobody
trades that way, and the whole of Phase 7 is about closing early. It is measured this way
because probability of profit is defined at expiry, so it is the only policy under which
calibration means anything, and because it is the pessimistic bound: a winner closed
early banks less, a loser closed early loses less, so held to expiry is the worse tail.

### A bug the demonstration caught, worth recording

The verdict originally compared the **top score bucket against the bottom bucket**. On a
synthetic sample built so the score genuinely worked, it reported no separation. The
quartiles read 67, 40, 95 and 87 percent: the top quartile was not the best quartile, and
comparing only Q4 against Q1 both threw away the middle half and picked an unlucky pair.

It now compares the **top half against the bottom half**, which uses every observation,
gives both intervals half the sample instead of a quarter, and answers the question
actually asked, which is whether high scores do better than low ones rather than whether
the very top beats the very bottom. A regression test pins the exact shape that failed.

Non overlapping intervals rather than a two proportion test, deliberately: it is the more
conservative of the two and will call a real effect inconclusive before it calls noise a
finding, which is the correct direction for a study whose purpose is to stop this tool
over claiming.

**One more thing the report now says out loud.** When the win rate is above 60 percent
and the mean profit is negative, it says so in a sentence: the losers are bigger than the
winners, win rate and expectancy are different questions, and short premium is designed
to win often. The demonstration produced exactly that shape (72 percent win rate,
negative expectancy), and it is the single most useful thing this report can tell
somebody.

### What is verified

Full suite and ruff green, 1143 passing. The pipeline was run end to end:

- `optscan record` scored the watchlist from stored snapshots and logged **903 real
  candidates** across six symbols. Those rows are left in the database on purpose: this
  is the same start-on-day-one problem as the IV history, and a candidate never logged
  when it was scored cannot be settled later.
- `optscan validate` on that log correctly reports nothing and says why, naming the
  earliest logged expiry (2026-08-21) so an empty study says when it will stop being
  empty rather than looking broken.
- Settlement is exercised offline in tests against hand computed values, including the
  defined risk floor, an unclamped naked short, and a breach that still made money.
- Both verdicts were demonstrated on synthetic samples shaped to separate and not to.

**Not verified: a real resolved outcome.** Nothing logged has expired yet, and nothing
can before 2026-08-21. The settling path is covered by an offline test with a fake
provider, and the first real run of `optscan resolve` is outstanding.

**Nothing is scheduled.** `optscan record` should run daily like the snapshot job and
`optscan resolve` weekly, and neither is registered. That is the obvious next small job
and it is not done.

---

## 2026-08-01: Phase 9, hardening

Hardening turned out to be mostly about the difference between a job that is configured
and a job that runs. Four of the six things below are failures that were already live
and silent.

### The scheduled task was registered with two settings that could have cost history

`schtasks /Create` was what registered the daily snapshot, and it cannot set the two
settings that decide whether the job actually runs. Its defaults are wrong for both, and
Windows own report of the existing task confirmed it: `Power Management: Stop On Battery
Mode, No Start On Batteries`, and no `StartWhenAvailable` element at all.

So the task as installed would not start on an unplugged laptop, and a machine asleep at
12:45 skipped the run entirely rather than deferring it. The old module docstring claimed
the opposite in its own words, that Task Scheduler "will start the job late rather than
not at all". That was simply false as registered.

This machine has no battery, so the first flag was inert here and no capture was actually
lost. That is luck rather than design, and the docstring premise was that this runs on a
laptop.

Registration moved to `Register-ScheduledTask` through PowerShell, which can set both.
Hand written XML would also work, but the settings element is order sensitive and a
schema mistake fails at install time on a machine nobody is watching, which is the same
class of problem being fixed.

**Running late is not free and is still better.** A deferred capture lands at a different
time of day than 15:45, and IV rank compares today against history sampled at a
consistent time, so a late run is a slightly worse observation. A missing session is
invisible and permanent. Take the worse observation.

### Settling could silently record a wrong outcome, permanently

`unresolved` selects `expiry <= today`, so a resolve run on expiry day itself asks the
vendor for a close it has not published. `_settlement_price` answered that question by
falling back to the previous trading day and attaching the note "expiry was a holiday".
Both the price and the label were wrong, `record_outcome` refuses to overwrite, and the
whole point of Phase 8 is that these rows are the evidence. An intraday run on a Friday
would have settled every expiring candidate against Thursday.

The fallback is now gated on the expiry not having been a trading day. A trading day with
no bar yet is missing, not shut: return nothing, leave the candidate pending, settle it
tomorrow. This is the same rule as everywhere else in the project, and the one place it
had been quietly broken. It was also completely untested, which is how it survived.

Scheduling follows from the fix rather than the other way round: `resolve` runs at 08:00
market time, before the open, when every expiry it can see is genuinely finished.

### `manage` is registered but not installed, and the reason is the snapshot

Every fifteen minutes through a session is 26 provider hits a day. yfinance publishes no
rate limit and throttles silently, and the thing that breaks when it throttles is the
15:45 capture, whose history cannot be rebuilt. That is the same reasoning that keeps
`OPTSCAN_LIVE_ENABLED` false. Spending an unmeasurable budget on alerts that currently go
to a JSONL file nobody reads is the wrong trade, so `--install` skips it and
`optscan schedule` prints why. `--install manage` is one command when the alerts go
somewhere real.

### Backups: two things that cannot be refetched, backed up two different ways

Everything this tool computes can be recomputed. The parquet captures and the sqlite
database cannot be refetched at any price, and they fail differently.

The database is copied through `Connection.backup` rather than the filesystem. A file
copy can catch a write in progress or catch the file without the write ahead log that
completes it, and the result opens fine and is missing rows. The copy is then opened and
integrity checked, because an unverified backup is a file that turns out to be unreadable
on the one day it matters.

The captures are mirrored incrementally on path plus size, since a capture is written
once and never edited. A daily run costs a day rather than the history.

**Rotation applies to the database copies and never to the mirror.** Keeping the last 30
dated copies is a real safety property: a corruption noticed a week late is still
recoverable. Rotating the mirror would mean deleting captures, which is the exact loss it
exists to prevent.

**A backup on the same drive is half a backup**, so the command says which one you have
rather than letting the word imply the stronger claim. The default is a sibling of the
repo, because a backup under `data/` dies with `data/`.

A Windows detail worth keeping: `with sqlite3.connect(...)` commits the transaction and
leaves the connection **open**. On Windows the open handle locks the file, and rotation
then failed to delete copies it had written itself. `closing()` is required, and a test
pins it. This was found by a test rather than by reasoning.

### The error boundary blanked the page it was added to prevent blanking

The UI already surfaced request failures well. Three things were missing: an unreachable
server had nowhere to appear, so the sidebar read "connecting" forever; every panel
repeated the same outage separately; and a render that threw took the whole tree with it
and left a white page, which for this tool is indistinguishable from a market with
nothing in it.

The first two were straightforward. The boundary was not, and it is worth recording why,
because the first version was verified in a browser and **failed in exactly the way it
was written to prevent**.

It was keyed on `` `${view}:${symbol}` ``, meaning to clear a crashed panel on
navigation. But the symbol resolves a moment after load, so the key changed while the
panel was crashed, the boundary remounted with no error, rendered the crashing child
again, and React escalated a boundary that kept failing to its parent. Blank page, by a
longer route. Keying on the view alone fixes it.

For the same reason there is no "try again" on the boundary, unlike on the fetch errors.
A failed request can succeed on a retry; a component that throws on this data will throw
on it again, so a retry offers the crash back. The button reloads the page, which is the
thing that actually changes the outcome.

### The coverage floor is per module, because a package total hid an 18

Analytics and screener together reported 90 percent. `screener/positions.py`, the module
that decides what a held position is actually worth, was at 18. The total was not wrong,
it was answering a different question, which is the same shape as every measurement
mistake in this log: an aggregate dominated by structure rather than by the thing being
asked about.

`scripts/coverage_floor.py` therefore gates every gated file separately and prints the
aggregate without ever gating on it. The lowest gated module is now `levels.py` at 85.

### What is still not true

Nothing here changes the two facts the README leads with. No outcome has ever been
resolved against real data, and the Tradier adapter has still never spoken to Tradier.
Phase 9 makes it more likely the jobs are running when the first expiry passes on
2026-08-21. It does not bring that date forward.

---

## 2026-08-02: a Desktop launcher for the dashboard

`optscan serve` was the only front door, and it needs a shell, the right working
directory, the venv interpreter rather than the 3.13 system one, and a window that stays
open. None of that is hard and all of it is enough friction to stop somebody glancing at
the board. `optscan shortcut` generates a `.cmd` and puts a shortcut to it on the
Desktop.

**A console window, not `pythonw`.** Starting the server with `pythonw.exe` would give
the most app-like result and the worst one: no console at all means no way to stop it
short of Task Manager. The launcher runs in a window started minimized, so it sits in
the taskbar and closing it stops the server. That window *is* the app, which is a model
somebody can reason about without being told.

**The launcher is generated, not committed.** It bakes in the configured host and port
and this machine's absolute venv path. Config is the single source for those everywhere
else, and a committed script with 8000 in it would quietly disagree with
`OPTSCAN_API_PORT` the day anybody changed it. It is gitignored and `optscan shortcut`
regenerates it.

**It checks health before starting anything.** A second double click should open a tab,
not a second server fighting for the port.

### Two things that only showed up by looking at the artifact

**The Desktop is not `%USERPROFILE%\Desktop`.** OneDrive folder redirection moves it, and
on this machine it is redirected to `C:\Users\kubic\OneDrive\Desktop`. Writing to the
unredirected path *succeeds*, and puts the shortcut somewhere the user never looks, which
is the worst kind of failure: silent and plausible. The known folder is asked for rather
than assumed.

**A brace failed to collapse and broke exactly one branch.** The health probe is built by
f-string, and its second fragment was not marked `f`, so `}}` reached the batch file
literally. PowerShell rejects it, the probe exits non-zero, and the launcher concludes
nothing is serving. Cold start looked perfect; the second double click would have started
a competing server. Found by reading the generated file rather than the generator.

`tests/test_launcher.py` now hands both PowerShell fragments to PowerShell's own parser,
and the icon to Windows' own icon parser. The generated artifact is the thing that has to
be right, and it is the thing that was wrong.

**The icon is written by hand.** A 32x32 BGRA `.ico` is a small header and a bottom up
bitmap, which is little enough format that taking on Pillow as a dependency for it would
be the larger cost. It reuses the dashboard's own palette so the two look related.

---

## 2026-08-02: the job health surface, and colour

`optscan schedule` reported all four tasks as "Ready". That is also true of a task that
has failed every morning for a week. `optscan status` listed captures only, so a broken
`record` was invisible. For a project whose central claim is that a missed day is
permanent, there was nothing that would tell you a day had been missed.

### Two sources, because a green exit code is not evidence of work

**Windows** knows whether it started a process and what it returned. **The new `job_run`
table** knows whether the work happened. Neither is sufficient and the gap between them
is the interesting part:

- A job that runs daily, finds nothing to do and exits 0 is a tick in Task Scheduler and
  a hole in the history.
- A job that dies on a bad config or a broken install never writes a log row at all, and
  looks exactly like one that was never scheduled.

So `health` reads both, and where they disagree the disagreement is the finding. The
sharpest case has its own verdict: Windows ran it, it exited non-zero, and it logged
nothing, which is reported as failing before it starts rather than as a missed run.

**A row is written at start and closed at finish**, which is why there are two writes
rather than one. A run killed by the execution time limit, or by the machine going away,
leaves a row with a start and no finish. One write at the end would leave nothing, which
is indistinguishable from never having run.

**The log wraps the CLI dispatch, not each job.** One place, uniform across every job,
and it catches the shape a job is most likely to fail in: returning a non-zero exit code
without raising. `Tracker.failed` exists so that path closes the run as a failure instead
of a success.

**Recording can never break the job.** Every entry point swallows its own errors and the
wrapped block runs even if the database cannot be opened at all. An observability layer
that can take down the thing it observes converts a survivable outage into a permanent
one, and permanent is the whole problem.

### Not crying wolf, which took three tries

A monitor that is wrong on weekends gets ignored on Mondays. Three separate versions of
this were wrong when run against the real machine, and each one would have shipped a
report that opened with a false alarm.

**The first** reported all five jobs as broken. The `job_run` table had existed for
ninety seconds, so of course nothing was in it. Fixed by recording
`meta.job_log_started_at` in its own migration and refusing to make any "it has not run"
claim about a scheduled time earlier than that. Before the log existed, a missing row is
missing evidence, not a missed run.

**The second** still reported `resolve` as missing. The floor was being compared at day
granularity, and resolve fires at 08:00 while the log had started at midday on the same
date. `expected_at` now carries the time of day, not just the date.

**The third** was a wrong constant. `_parse_task_time` discarded Windows' never-ran
sentinel using a cutoff of 1980, on the assumption that the sentinel is 1899-12-30. What
`Get-ScheduledTaskInfo` actually returns is **1999-11-30**, which sailed through and made
three tasks that had never run look like they had run successfully in the last century.
Caught by querying a freshly registered task rather than trusting the documentation.

The counterpart matters as much as the silence: when something is genuinely late the
number of missed sessions is stated, counted over the market calendar. "late" is a shrug.
"3 sessions missed" is a decision.

### Colour

Not decoration. Every command here prints a table where one or two rows are the point,
and the whole project is about making a bad state visible rather than plausible.

Three rules, and the first two are about restraint. **Never when the output is not a
terminal**, because escape codes in a redirected file are corruption. **Never when
NO_COLOR is set**, read in `config.py`, which remains the only module that touches the
environment. And **colour is never the only carrier**: every state that has a colour also
has a word, because about one man in twelve cannot distinguish red from green and a log
pasted into a chat window arrives as plain text.

`console.pad` exists because `f"{painted:<9}"` pads to the length of the escape sequence
rather than of the word, so a coloured column is a misaligned column. Anything laying out
a table pads with it.

Windows Terminal handles ANSI; the older conhost does not unless the console mode is set,
and unset it prints the codes literally, which is worse than no colour. The flag is set
explicitly and colour is off if that fails.

One incidental finding: the environment this was developed in sets `NO_COLOR=1`, so the
default-mode test passed or failed depending on who ran it. The colour tests now clear it
rather than inheriting it.

---

## 2026-08-02: first contact with Tradier

A real sandbox token exists. The adapter written in Phase 5 had never spoken to Tradier;
it has now, and all four market endpoints work on the first try. `get_events` refuses as
designed, because Tradier publishes no corporate calendar.

### The recapture instruction in the fixtures README was wrong

It said: when a token exists, recapture these against the sandbox and delete this
paragraph. Following it would have deleted the reason the fixtures exist.

The constructed files carry deliberate damage that a healthy response never contains: a
row with no `option_type`, a row with no `strike`, a bar whose close sits outside its own
high and low. Those pin the adapter's refusals. A real capture has nothing to say about
any of them, so overwriting would have quietly removed the entire refusal suite while
every remaining test still passed.

Both sets now exist. The constructed set tests what the adapter does with broken input.
`tests/fixtures/tradier/live/` tests that the input it will really get is the shape it
expects, and asserts field by field that every name the constructed fixtures assume is
actually on the wire. Without that assertion a vendor rename breaks the adapter silently
while the constructed fixtures keep passing forever, because they encode the old name too.

The chain capture is trimmed to 44 of 340 rows, a near the money window plus both wings,
because the full response is 464 KB. Rows were kept or dropped, never edited.

### What the wire confirmed, and the one thing it did not

The risky assumptions were right. One symbol really does return an object and two really
do return an array, which is the quirk most likely to have been a documentation artifact.
`unmatched_symbols` is the real shape for an unknown ticker. The greeks block and the
history day match the documentation exactly. Every chain field the adapter reads is
present, and the wire carries eight more it ignores.

One documented quote field, `lot_size`, is **not** returned. It is unused: contract size
is read off the chain row, where it is present and is 100.

### The vendor's own volatility is unusable, and now that is measured

`mid_iv` comes back as **10.0** on the deep in the money puts and **0.0** on the deep in
the money calls. Both are a solver that failed and published its clamp. The 500 put,
quoted 0.00 by 0.01, comes back at 1.37: a 137 percent volatility manufactured by a one
cent ask, which is the sixth instance of the constant offset trap appearing in a vendor's
own numbers rather than in ours.

The model already refused the zeros through `_drop_junk_iv`; this is the first real data
to exercise it. The 10.0 survives, because it is under the plausibility ceiling. Nothing
reads it as a volatility, and that is the whole reason the project solves its own and
keeps `vendor_iv` only to disagree with the solve.

`smv_vol` is worse as a candidate source: it is constant across a whole region of the
chain, 0.1557 for every strike from 500 to 510 and 0.0598 for every strike from 940 to
950. It is a smoothed surface value, not a per contract measurement.

### Real crossed quotes exist, on liquid strikes

The captured chain contains two crossed rows near the money: the 744 put at 1.58 by 1.12
and the 745 call at 4.56 by 3.48, both bid above ask, on a closed market. `is_crossed`
was written against the possibility. It is now evidence, which promotes every refusal
downstream of it from defensive to load bearing.

Also worth recording for the closed market case: `quote.price` prefers the mid, and
Tradier's stale weekend bid and ask put the mid at 744.36 while `last` and the Friday
close agree at 747.03. A 0.36 percent error in spot, on a closed market only, in the one
input every probability is a function of. Not acted on: the snapshot job runs at 15:45
inside the session, where the two sided market is real.
