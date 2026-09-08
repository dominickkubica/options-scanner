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

---

## 2026-09-07: the price chart

Rebuilt the underlying price chart. `Candles.jsx` is gone and
`frontend/src/components/chart/` replaces it: `PriceChart.jsx` draws, `indicators.js`
is a registry, `aggregate.js` resamples, `theme.js` bridges the CSS palette into the
canvas. Intervals 1D, 1W and 1M; windows per interval; candles or area; MA20, MA50 and
MA200 toggleable and off by default.

### Three removals, not additions

**There is no floating tooltip.** Hovered values are written into a fixed header above
the canvas and it snaps back to the last bar on mouse out. A tooltip that follows the
cursor covers the candles the reader is pointing at and moves the numbers to a
different place on every frame.

**There are no vertical gridlines and no axis borders.** A price chart already has a
strong horizontal structure, and every vertical rule competes with the candles for the
same lines. This was the single largest visible declutter.

**The price axis is locked to autoscale.** Dragging the axis to squash or stretch the
price range buys nothing on a daily chart, and it lets a stray gesture flatten a thirty
percent move into a straight line. That is the chart lying because of a slip of the
mouse. Horizontal zoom stays, on the time axis.

### The wheel belongs to the page

Found by hitting it rather than by reasoning. Scrolling the page with the pointer
anywhere over the chart zoomed the chart instead, which both blocked the scroll and
left the view drifted away from the latest bar with no visible cause. A chart may
capture the wheel when it owns the screen; this one is a panel stacked among five
others. `handleScale.mouseWheel` and `handleScroll.mouseWheel` are both off, and zoom
is reachable by dragging the time axis.

### `days` is a bar count, not calendar days

The history endpoint's `days` parameter returns that many **bars**. Measured against
the running API: `days=730` comes back with 730 sessions starting 2023-10-09, which is
nearly three years. The first version of the window table treated the parameter as
calendar days, so the button reading "6M" was showing 8.7 months and "5Y" was showing
7.2 years, a systematic overstatement of about forty five percent on every window.

Windows are now sized in sessions off a `SESSIONS_PER_YEAR` constant, so the label is
the calendar span the sessions actually cover. This is the constant offset trap in its
smallest form: a number that was wrong by a fixed ratio everywhere, and therefore
looked internally consistent.

### Candles degrade to a line past 400 bars

At two years of daily bars a candle is under two pixels wide and the chart is a smear
with no readable body or wick. Past 400 displayed bars the area series is drawn
whatever the toggle says, and the chart states the count and the reason. Chosen on
total displayed bars rather than on the visible range: reacting to zoom would flip the
rendering mid gesture, which is worse than a stable rule the reader can predict and
step around by picking a coarser interval.

### A partial moving average is never drawn

`sma` leads with nothing until its window is full. A twenty bar average computed over
seven bars is a different statistic wearing the same label, and on the same axis it is
indistinguishable from the real thing. Where an indicator cannot draw at all the chip
stays visible and says why, for example `MA200 needs 200 bars, have 126`, rather than
disappearing. A chip that vanishes reads as a bug; one that states the shortfall reads
as an answer.

MA200 is unavailable on every monthly window and that is correct rather than a cap to
raise: 200 months is about seventeen years.

### Weekly and monthly candles are resampled, not fetched

The API serves daily bars and nothing else. Aggregating in the browser costs one pass
over data already in memory, while a second interval on the wire would mean more
requests against a vendor that throttles silently and whose throttling would take the
15:45 snapshot job down with it. The existing `(symbol, days)` cache is doing the rest.

Two rules inside the aggregation. **A partial trailing period is kept and unmarked**,
because the current week or month is real and forming and dropping it would hide the
most recent price action. **A period containing any unknown volume has unknown volume**,
because summing only the bars that carry a number publishes a partial total as if it
were the period's, which is the null-is-not-zero rule in its most tempting form.

### Indicators are a registry

Adding one should mean adding an entry to `indicators.js` and touching nothing else.
An entry declares its plots and computes them; `PriceChart` iterates the registry,
creates whatever series each entry asks for, and knows nothing about what any of them
mean. Multiple plots per indicator is the part that matters: it is what lets a
Bollinger band or a MACD arrive later without reopening the chart component. An
oscillator wanting its own pane is the one extension the registry cannot absorb alone.

### Intraday deferred, deliberately

1m through 1h would need the provider interface extended, an `interval` parameter on
the history endpoint, and yfinance-specific window caps. The real cost is request
volume against a silently throttling vendor, and the 15:45 snapshot builds a history
that cannot be backfilled at any price. The interval control is shaped to take intraday
later as wiring rather than a redesign. If it is built, Tradier's `timesales` is the
safer source, at the price of a sandbox token that expires unless the account is
funded.

### One bug worth naming

The interval state setter was first written as `setInterval`, which shadows the global
timer function. Renaming the setter left one call site pointing at the real
`setInterval`, so clicking an interval scheduled a no-op timer with a string argument
and silently did nothing, with no error anywhere. Do not name a state setter after a
global.

### Palette rebased on Webull, same day

The 2026-09-05 visual pass took surfaces, radii and type from TraderVue. The colour
now comes from Webull mobile instead, measured off screenshots rather than guessed.
Three changes carry the whole difference and none of them is structural:

- **The ground is darker and blue rather than grey.** `#101626` to `#0a1020`, with the
  panel ladder moved to match. This is most of the effect on its own.
- **Up is a bright mint, not a sage green.** `#46b17b` to `#1fd9a0`.
- **Down is a vivid pink, not a salmon.** `#d9635f` to `#ff3b69`.

The accent also went more saturated, `#4c9be8` to `#3b7ded`, and it stays blue for the
reason recorded in the last pass: green already means profit here, so a green primary
button beside a green P/L figure would be one signal doing two jobs. Webull's own
accent is blue, so the reference agrees.

Incidental benefit worth recording against the Journal calendar note: mint against
pink separates further under deuteranopia than sage against salmon did, because the
pair now differs in lightness and in the blue channel rather than mostly in hue. The
"colour is never the only carrier" rule is unchanged and every signed value is still
written out.

Two sources of drift were closed in the same change rather than left to rot. The hand
rolled SVG charts held nine hardcoded hex literals for colours the tokens already
named, so they would have kept the old palette while everything around them moved;
they now read the tokens. And `chart/theme.js` carries a fallback table for the case
where a token resolves empty against the stylesheet, which is a race rather than a
missing value: a stale entry there would repaint the chart in last month's palette on
exactly the frames nobody is watching. It mirrors `:root` and has to keep doing so.

---

## 2026-09-07: the broker ledger, and what importing real fills revealed

`optscan import` reads a Robinhood activity export into an append only ledger, and
`optscan trades` reports what the account actually did. New: `models/broker.py`,
`imports/robinhood.py`, `analytics/ledger.py`, `storage/ledger.py`, migration 6.

### A ledger, not positions

Statement rows are stored raw and positions are derived. The reason shows up on the
second import rather than the first: the next export will overlap this one by a month,
and an importer that wrote positions directly would have to decide, per position,
whether it had seen it before. Against raw rows with a stable identity a re-import is a
no-op, verified both in a test and against the real file.

Identity is `(source, digest, dup_index)`, not `(source, digest)`. Two genuinely
identical fills on one day are two trades and a content hash cannot tell them apart, so
the importer counts occurrences within a file. Without the index a real trade quietly
disappears; without the digest the account doubles.

### Three things in the format that would each have produced a wrong number

**Accounting parentheses.** `($34.04)` is cash out. Parsed naively every debit becomes
a credit.

**Two description shapes.** A trade is `TSLA 9/4/2026 Call $357.50`; an expiration is
`Option Expiration for IBIT 8/14/2026 Call $37.00`. A parser written against the first
alone loses every expiration and leaves the position open forever.

**The trailing S, which is not decoded at all.** Robinhood marks one leg of an expiring
spread `1S`. The first implementation read it as "short" and produced six phantom open
contracts. In all three expiring spreads in the reference file the S sat on the leg that
was bought, and also on the higher strike, and three cases cannot separate those two
rules. So the S is captured and never used: an expiration's direction comes from the
holding the ledger says was open at that moment, which is right whatever the S means and
stays right if Robinhood changes it. `signed_quantity` returns None for an expiration
rather than guessing, and the resolution lives in `analytics/ledger.py`.

### Fees are measured, and only where they can be

Price times multiplier times quantity does not equal the amount, and the gap is the
regulatory fee: 4 cents a contract on buys, 6 on sells, $24.35 across 380 legs. That is
the cost input every expectancy question needs and it is now a measurement rather than a
config guess.

The same arithmetic on shares is nonsense, and it announced itself: fourteen of the
thirty four share rows produced a **negative** fee. Robinhood rounds the displayed price
to two decimals while the amount is exact, so `HTZ Buy 100 @ $2.58` shows a gross of
$258.00 against $257.50 actually paid. The fill was $2.575 and the 50 cent "fee" was the
rounding, which fractional share quantities make worse. `_fee` now returns None for
anything that is not an option trade: not zero, because the fee is unknown rather than
absent, and an unknown averaged in as zero understates every cost built on it. Eleventh
instance of the constant offset trap, caught by the residual having the wrong sign.

### An unknown transaction code raises

Robinhood has codes this export happened not to contain, assignment and exercise among
them, and both move real contracts. Skipping what is not recognised would produce a tidy
profit figure with legs missing from it, which is worse than a crash because nothing
would ever say so.

### What the first import showed

337 of 380 option legs expired the day they were opened, and 311 of 380 were QQQ. The
screen's DTE band was 21 to 60, so **it had never once surfaced a trade this account
would take**, and the validation study has been calibrating a screen nobody trades. The
band is now 0 to 45, in both `screen.yaml` and the `DteFilter` defaults so the two do
not drift.

Realized, from real fills: options +$77.65 over 177 closed round trips, equities
-$17.87 over 9, one position still open and excluded. QQQ alone is -$38.53 over 21
trading days, 14 up and 7 down, median day +$18.40 against a mean of -$1.83, worst day
-$241.14. The mean sits 0.13 standard errors from zero. Days are the unit of
independence here, so that is roughly 21 observations, barely past the 20 cluster
minimum `calibration.py` already refuses to conclude below.

### Five tests changed, and why that was the right fix

Five tests leaned on the shipped 21 day floor to produce an empty result, one of them
saying so in its own docstring. They were testing that an empty table explains itself,
which is still worth pinning, so each now states the window it wants rather than
inheriting whatever ships. A test about explanations should not fail because a default
moved.

One incidental repair: `test_live_fetches_from_the_provider_instead_of_disk` had been
failing on date drift, its frozen expiries having aged past the 21 day floor. Widening
the band fixed it. It was a real failure with a real cause, and the cause was the floor.

### Open

The Journal still reports `opportunity_outcome`, which is the screen held to expiry with
no fills and no slippage. That is the wrong surface for an account that closes intraday,
and the ledger is now the better source. Keeping the two apart matters more than merging
them: 2,060 hypothetical candidates showing +$426,644 must never share an equity curve
with 177 real round trips showing +$77.65.

### The journal now reports the ledger, and the old surface was worse than wrong

`GET /api/journal` reads imported broker statements instead of `opportunity_outcome`.
The old surface reported candidates the screen surfaced and `optscan resolve` settled by
holding them to expiry, with no fill, no slippage and no early management. Against an
account that closes almost everything intraday it was measuring something nobody did.

Its headline was **+$426,644 across 2,060 "trades"**, which is the sum of 2,060
hypothetical single contract positions that were never held at the same time. It is not
a large number, it is a meaningless one, and a calendar and an equity curve are the two
most persuasive objects this app can draw. The calendar keyed on settlement dates, so it
lit up three squares and none of them were days the account traded.

Those rows still exist and still matter: they are the calibration sample for the score,
and `optscan validate` is their report. They are simply not a journal.

**The grain is `(symbol, closing day)`, not the contract.** `build_trades` cuts the
ledger per contract because that is the arithmetic, but a four leg condor closed in one
afternoon is four contracts and one decision. The day is also the honest unit of
independence, since everything traded in one underlying on one day shares one market
move. So trade count and cluster count are equal here by construction. That is the
point: the screener study has to widen every interval from rows to clusters to avoid
manufacturing a finding, and here there is nothing to widen because the grain was chosen
to be the cluster.

Two smaller things fell out of it. `_band` now tolerates a None rather than comparing it
against a float, because a trade taken at a broker has no score and the breakdown should
come back empty rather than raise. And the held-time bands were retuned from 0-20,
21-30, 31-45, 46-60, 61+ (the thirds of the old 21 to 60 screen window) to same session,
overnight, 2-4, 5-9, 10+. The old ones put every entry in a single bucket against an
account that trades 0DTE, which is a breakdown that breaks nothing down.

First real read: 39 trading days, +$59.78, win rate 67% (51% to 79%), expectancy $1.53 a
day with an interval of -$14 to +$17 that includes zero. Worth watching as the sample
grows: same session entries are -$32.47 over 31 days while the handful held overnight or
longer are +$92.25 over 8. Eight is not a sample and this is not a finding, but it is the
opposite of what the account is doing most of.

---

## 2026-09-07: a downloaded volatility history, and the two traps it sprang

The user signed up for Market Chameleon and exported its daily history for QQQ and
AAPL. Each file is 3,188 sessions, 2014-01-02 to 2026-09-04, and carries a column no
provider this project can reach has ever offered: **IV30, the vendor's own 30 day
constant maturity implied volatility, for every session.**

That column is the thing this project has been blocked on since it started. IV rank
needs 20 daily observations and the tool had 8 after weeks of capture, so
`component_iv_rank` was null on all 10,020 recorded candidates and every rank in the UI
read `insufficient`. The Phase 8 note calling a historical backfill "the highest
leverage purchase available" priced it at ThetaData's 40 dollars a month. This was free.

### What was measured before anything was built

Against the full QQQ file, because the standing rule here is to count hits against real
data before believing anything:

- **3,185 of 3,188 rows carry an IV30**, ranging 8.56 to 79.36, median 17.63. Only 7
  consecutive identical values in 3,187 pairs, so it is a real daily series rather than
  something forward filled.
- **`Adj Close` differs from `Close` on exactly 52 rows**, which is exactly 12.7 years
  of quarterly dividends. `Change` reconciles with close to close on all but those same
  52. That reconciliation is what confirms the row ordering and that the adjustment is
  real, and it is the reason to trust the rest of the file.
- **The same three sessions are blank in both tickers** (2014-07-02, 2014-07-08,
  2015-06-23), so those are holes in the vendor's own pipeline and will appear in every
  symbol. They are stored as None. A zero would be a published number.
- **Open interest begins 2018-01-30**; option volume is complete throughout.
- **IV30 exceeds HV30 on 60.1% of days, median ratio 1.07.** Worth recording because it
  was asserted in conversation to be "nearly everywhere" before it was measured. The
  variance risk premium is real but modest, and comparing IV/HV against 1.0 would still
  call 60% of all days rich, which describes the market rather than finding anything.
- **Put/call volume has a median of 1.39 on QQQ and 0.578 on AAPL.** Same shape of
  trap: the raw ratio is a property of the instrument, not a signal about the day.

### The format's one real hazard

**There is no symbol column anywhere in the file.** The ticker exists only in the
filename. A renamed download files twelve years of one instrument under another's name,
every number downstream is wrong, and nothing anywhere contradicts it. So `parse_file`
requires the symbol as an argument rather than inferring it, and the import refuses to
overwrite: a session already stored whose values disagree is counted, never applied.
The two causes are separated by shape, since a vendor revision touches a handful of
sessions and a wrong ticker disagrees on nearly all of them. Verified by importing the
AAPL file as QQQ: 3,188 of 3,188 conflicted, the warning named the likely cause, and
nothing was written.

### Trap ten, caught because the data arrived

`analyze_snapshot` ranked `expiries[0].atm_iv`, the **front** expiry, against a history
built at 30 day constant maturity. Those are two different quantities: short dated ATM
vol is mechanically elevated. Measured on the stored captures, the front expiry solves
to **43 vol points on QQQ and 83 on AAPL** at 0 DTE, against 30 day points of 17.2 and
25.5, and against a one year range topping out near 30. That ranks full on the calmest
day of the year.

It had never fired because no history was ever long enough to produce a rank at all.
Importing 3,185 observations is precisely what would have made it live, so it was fixed
in the same change. `atm_iv_near_dte` reads the vol at the history's own tenor and
returns **None** outside a half-to-double band rather than the nearest available,
because a rank from a mismatched tenor is worse than no rank: nothing downstream can
tell it was mismatched. When that happens the UI now gets a sentence saying which of the
two reasons applies, since a gauge that renders nothing and explains nothing reads as a
broken feature.

### Trap eleven, which this session created and then removed

The pooling rule says an IV history belongs to one vendor. The obvious reading is about
concatenating two series. The subtler form appeared immediately: the *current* value was
still the locally solved ATM vol while the *range* was now Market Chameleon's.

Measured on 2026-09-02, the two vendors disagree by **-3.2% on QQQ and +2.9% on AAPL**,
in opposite directions, so there is not even a systematic bias that could be corrected.
Small, until it goes through a rank, which divides by a narrow 52 week range: QQQ's rank
came out **0.14 cross vendor against 0.181 self consistent**, a 29% relative error from
a 3% difference in vol. `vendor_iv_history` now splits the vendor's own latest reading
off as `current`, which also keeps today out of the range it is ranked inside.

### Trap eleven's second half, and the reason a ranking is not a score

Then the real one. `composite` drops a component it cannot compute and renormalizes the
remaining weights, which is right within one symbol: it stops an unavailable input from
dragging every score toward zero without changing any order.

Across symbols it is badly wrong, and it *became* wrong the moment a history existed for
two tickers and not the other four. Renormalizing a missing component silently replaces
it with the average of that candidate's other components, which for a candidate the
screen already likes is a high number. Measured on the live config against an otherwise
identical candidate:

    no IV history at all      0.9387
    with a real iv_rank 0.18  0.7870   (-0.152)
    break even                0.9387

**Below an IV rank of 0.939, importing a volatility history lowers a symbol's score.**
QQQ's honest 0.18 cost it enough to drop out of the Best plays list entirely, for the
sole reason that its data exists. That is the recurring bug in a new place: a structural
difference, here which components happen to be computable, read as a difference in
quality.

`harmonize_scores` rescores a list that will be ranked against itself over the
**intersection** of components its members share, and every affected row carries a
warning naming what was dropped. The intersection throws away real information about the
symbols that have more, and that is the correct direction anyway: a comparison is only as
good as its weakest common ground. Downloading the remaining four tickers is what widens
it back, which makes that a data errand with an obvious fix rather than a scoring problem.

**It is applied for display only, never when recording.** `jobs/validate.py` passes no
config, on purpose: a recorded score must depend only on the candidate, never on which
other symbols happened to be in the same batch, or the validation study is calibrating a
number that moves for reasons the market did not.

### Smaller things

- **The two histories are chosen between, never merged.** More observations wins, which
  is 3,185 against 8 and will not be close within the tool's lifetime. The loser is
  discarded rather than kept as a fallback for missing days.
- **`optscan serve` and the CLI now share one loader**, so the dashboard and the terminal
  cannot rank a symbol against different history.
- **The IV rank panel's empty state was a lie as of today.** It said a rank "needs months
  of daily captures and cannot be backfilled", which was true this morning. It now points
  at `optscan import-history`.
- **Best plays claimed a 60 day ceiling** while `screen.yaml` had said 45 since the DTE
  band moved this morning. The number was hardcoded in the JSX. `ScanOut` now echoes
  `max_dte` and the heading reads it.

### What this does not do

IV30 is a single number, not a surface: no skew, no term structure, no per strike vol.
It supports a rank, an IV/HV comparison and vol regime context. It cannot price a 20
delta put and it cannot touch 0DTE, so it does **not** on its own enable a backtest with
modelled credits. The scoring recalibration is still the next job, and is now better
armed: `component_iv_rank` has stopped being null, which leaves `component_premium`
saturated at 1.000 on 88.6% of rows and `component_event_risk` a literal constant.

Downloads are limited to 2 per 24 hours on the trial. SPY and IWM are the next two.

---

## 2026-09-07: the Alpaca adapter, and five things the docs get wrong

Alpaca paper keys arrived, free Basic plan. `providers/alpaca.py` implements the full
`MarketDataProvider` interface plus `get_option_bars`, which is outside it.

Everything below was measured against the live API rather than read. That was not
diligence for its own sake: **five behaviours differ from the documentation and every
one of them fails silently**, returning 200 and a plausible number.

### 1. The host people paste is the wrong host

`paper-api.alpaca.markets` is the trading API. Market data is on
`data.alpaca.markets`. A data request to the trading host 404s with "Not Found", which
reads like a delisted symbol.

Both are used deliberately: quotes, bars and chains come from the data host, and
**open interest exists only on the trading host** at `/v2/options/contracts`. The
screen filters on open interest, so a chain is assembled by joining the two per
contract. 99 of 100 sampled QQQ contracts carried one.

### 2. The feed name differs per endpoint, and each is rejected where the other works

Measured on QQQ:

    endpoint    iex              sip                    delayed_sip
    bars        200, 376k vol    200, 23.6m vol         400 invalid feed
    snapshot    200, 553k vol    403 recent SIP         200, 33.2m vol

So bars take `sip` and snapshots take `delayed_sip`, and neither is a preference: the
other value is an error or a wrong number.

**`feed=iex` is the trap.** It returns 200 with about 1.8 percent of consolidated
volume and a close six cents off, and nothing in the response says which exchange it
covered. A screener reading it would see plausible prices and volume wrong by 50x.

`delayed_sip` is fifteen minutes behind and is still the right choice for a quote here:
this tool reads the last stored capture rather than deciding a fill, and it already
reports quote age on every screen. Being late is visible; being 2 percent of the market
is not.

### 3. The contracts endpoint silently answers for four expiries

The one that would have done real damage. `/v2/options/contracts` with no expiry
bounds returns 1,606 QQQ contracts across exactly **4 expiries**, 200 OK, with no
`next_page_token`. It is not pagination: `limit=100` walks 17 pages to the same 1,606
rows. With explicit bounds, 90 days gives 20 expiries and two years gives 33.

An undocumented default window, and the result looks complete. The adapter now always
sends `expiration_date_gte`/`lte`, and a test pins that it does. Without it a screener
would conclude QQQ lists options for the next four days.

### 4. Greeks and implied vol ARE on the free feed

The docs imply they need OPRA, and a first probe agreed, because a `limit=2` chain
request returns the two deepest contracts, which have never traded and so carry a
quote and nothing else. Filtered to a real expiry, snapshots carry `greeks`,
`impliedVolatility`, `dailyBar`, `latestTrade` and `latestQuote`.

`vendor_iv` is stored and **not used**, exactly as with Tradier, whose `mid_iv` was
measured as unusable. This project solves its own vol from the mid. Alpaca's number is
kept only so the two can be compared, which is now possible for the first time.

### 5. OPRA is a signature, not a subscription

`403 OPRA agreement is not signed`. That is a different problem from a bad key and is
reported as such, because the remedy is a form on Alpaca's site rather than a new
credential. The generic 403 path says something different again.

## What Alpaca cannot do, which decides what may be built on it

**There is no historical option quote endpoint.** `/v1beta1/options/quotes` is a 404 at
every parameter combination tried; only `/quotes/latest` exists. Historical option
*trades* are shallow too: a 2024 window returns `{}` while a two day old one returns
trades.

Historical option **bars** do work, back to at least 2024-01-18, earlier than the
documented February 2024. Including **minute** bars, including on 0DTE contracts.

So a historical option chain can be reconstructed at **trade** prices and never at the
mid. That is materially weaker than this project's live path, which prices at the mid
and refuses a crossed or absent quote, and it is weakest exactly where it would be
leaned on hardest: a thin contract's last print can be hours stale and on whichever
side happened to lift. It does not restore the paused 0DTE backtester on its own; the
sample size objection recorded there is untouched by better data.

Historical **stock** quotes, by contrast, are full NBBO tick data back to at least
2024. The asymmetry is the whole story: stocks are richly served, options are not.

## The event calendar, half of one

`get_events` is implemented from `/v1/corporate-actions`, which publishes
`cash_dividends` with `ex_date`, `rate`, `record_date` and `payable_date`, and flags
specials. That serves `exclude_early_assignment_risk`, which is a real gap closed: a
short call in the money over an ex date is the classic assignment.

**`earnings_date` stays None and is never guessed.** Alpaca has no earnings calendar,
and `exclude_earnings` is the heavier of the two filters. The screen already handles
the absence by widening and saying it could not check. A screen that believes it
checked earnings and did not is worse than one that knows it could not.

Measured: NVDA returns 2026-09-10 at 0.25. AAPL and QQQ return None, because their next
distributions are not declared yet, which is the honest answer rather than a missing one.

## Throughput, measured

- Chain, 0 to 45 DTE: QQQ 4,992 contracts in 5 requests and 3.4s; AAPL and NVDA about
  1,250 in 2 requests and 1.2s. The chain endpoint does take `expiration_date_gte/lte`.
- **Daily bars take up to at least 58 symbols in one request**, 0.7s.
- Minute bars go back to **2016-01-04** on SIP; 2015 returns empty. 30 symbols for one
  session is 20,197 bars in 3 requests.
- Daily bars carry `v` share volume, `n` trade count and `vw` VWAP.
- Basic plan rate limit is 200 requests a minute, reported in `x-ratelimit-*` headers
  and logged only when nearly spent.

At those rates a 600 symbol option capture is roughly 3,000 requests, about fifteen
minutes, which is not the constraint. Storage is.

---

## 2026-09-07: bulk price history, and a universe that admits what it is

`optscan prices sync` fills `vendor_daily` for many symbols at once. First real run:
**293 of 294 symbols, 693,974 sessions, 2016-09-09 to 2026-09-04.**

### The table was already right

`vendor_daily` was built the same day for a Market Chameleon download, and it turned
out to be the shape of any vendor's daily series: identity is
`(source, symbol, session_date)` with the source on it, so Alpaca and Market Chameleon
coexist for the same ticker without ever being pooled. AAPL now holds both, 2,511
sessions from Alpaca and 3,188 from Market Chameleon, and nothing merges them.

Migration 8 adds one column, `trade_count`.

### Three volume fields, because volume alone answers the wrong question

Alpaca's daily bar carries `v` shares, `n` trades and `vw` VWAP. All three are stored.

Volume by itself cannot distinguish 30 million shares in 500,000 prints, an ordinary
session, from the same 30 million in 5,000, which is a handful of blocks.
`VendorDailyBar.average_trade_size` is volume over count and is only computable because
the count is kept. Measured on AAPL 2026-09-04: 39,788,274 shares in 907,132 prints,
VWAP 321.20, 44 shares a print.

### Batching is the feature, not an optimization

Alpaca's bars endpoint takes a comma separated list. Measured: **58 symbols in one
request in 0.7 seconds**, against roughly 35 seconds and 58 requests the naive way. At
200 requests a minute the difference decides whether a few hundred symbols of multi
year history is two minutes or an hour, and the daily option capture needs that budget
too.

`get_daily_bars_bulk` sits outside `MarketDataProvider` deliberately. That interface is
one symbol at a time, which is right for a screener reading one chain and wrong here.

### A symbol that does not come back is reported, never dropped

The vendor returns bars for what it knows and omits the rest with no error and no
mention. Measured: a 58 symbol request returned 57. Delistings, ticker changes and
typos are indistinguishable from here and all three need a human.

So every requested symbol producing no rows is collected and named. The first full run
surfaced exactly one, `LYNAS`, which is an ASX line rather than a US ticker. Its ADR
`LYSDY` then failed too, because this plan refuses OTC outright with
`403 subscription does not permit querying OTC data`. **No OTC name can ever be synced
here**, so the entry was removed with a comment rather than left to fail forever.

That is the whole argument for the report: a hole in a price history is invisible in
every chart drawn over it.

### The universe file says out loud that it is not an index

`universe.yaml` holds six curated groups, 294 unique symbols: `etf`, `tech`, `metals`,
`uranium`, `rare_earth`, `sp500_large`.

Curated because there is nothing to derive them from. Alpaca publishes 14,277 active US
equities and **carries no sector, industry or index field on any of them**. Somebody
typed these lists.

That matters more than it sounds, and the file's header and the module docstring both
say it: a group called `sp500_large` reads like index membership and will be described
that way in conversation within a week. It is not. It is the constituents somebody
remembered to type, **which is exactly the set that did not get dropped for performing
badly**. Any aggregate over it carries that survivorship. `meta.date_checked` is the
only thing that says how stale it is, and `optscan prices groups` prints a warning past
a quarter, because index changes cluster around quarterly rebalances.

### Two bugs the tests found, both real

Writing the test for "an incoherent bar is dropped" found that it was not:

- **`VendorDailyBar` had no coherence validator.** `PriceBar` refuses a close outside
  its own high and low; this model accepted anything. Written for a hand downloaded
  file where the arithmetic is the vendor's and reliable, it is now filled from an API
  in bulk where a malformed row is one of thousands nobody reads, and a bad close would
  sit in the history producing a realized volatility wrong by an unrecoverable amount.
  It now enforces the same contract, and refuses rather than clamping: a repaired bar
  is a number with no provenance.
- **`import_daily_bars` did not guard a session appearing twice in one call.** It reads
  the existing sessions once, so two incoming rows for the same day both queue and the
  unique index turns it into an `IntegrityError` partway through a batch. The Market
  Chameleon path never hit it because its parser raises on a duplicate session inside a
  file; the bulk API path has no parser. The guard now lives in storage, where both
  paths pass through.

### What is not done, and is the thing to think about next

**Nothing has been pointed at the screener yet.** This is price history, not option
chains, and the watchlist is still six symbols.

Expanding the *screen* to hundreds of symbols is a different decision from expanding
price history, and it has a statistical cost that should be measured before it is
taken: Best plays ranks the watchlist and reports the top candidate, so the best play
out of 600 symbols is a maximum over a hundred times more draws than the best out of 6.
That is a selection effect, it will make the top of the list look dramatically better
without anything improving, and it is the same family as every other entry in this log.
The cluster reasoning in `calibration.py` already exists to handle exactly this shape
of problem and has not been applied to it.

---

## 2026-09-08: a home page, ticker search, and pinning

The sidebar listed six symbols as buttons. That was the right shape for six and stopped
working the moment the catalogue reached 293, so this adds a landing page, a search box,
a group browser, and the ability to pin a ticker from the UI.

`catalogue.py`, `api/routers/catalogue.py`, `views/Home.jsx`, `views/Browse.jsx`,
`components/SymbolSearch.jsx`.

### The distinction the whole thing is built around

Three facts get confused constantly once there are hundreds of tickers, and separating
them is most of the value here:

  - **In a universe group.** A label somebody typed. Not data.
  - **Has price history.** Daily bars are stored. Enough to chart, to compute realized
    vol, to draw levels. **Not enough to screen.**
  - **Has captured option chains.** The only one that lets the screener produce a
    candidate.

Right now that is 293, 293, and **6**. A search result showing only "found" would invite
somebody to search a ticker, click it, and meet an empty screener with no explanation.
So every row carries a status phrase rather than a checkmark, `screenable` is its own
field, and the home page's coverage line puts the accent on the smallest number.

### Pinning is honest about what it does not do

Adding a symbol to the watchlist makes the **next** `optscan snapshot` run fetch its
chains. It does not conjure a chain that was never captured, and the earliest a newly
pinned symbol can be screened is after that run. Every add returns a note saying so, the
home page raises it under "Needs attention", and the pinned card is drawn **outlined
rather than filled** until a capture exists.

That outline is the same signal Best plays uses for a blocked near miss, and reusing it
was deliberate: both mean "a real thing whose data is not ready", and the app now has
one visual vocabulary for that rather than two.

Removing a symbol stops future captures and deletes nothing. A chain from a day that has
passed cannot be captured again, so a removal that cleaned up history would be
irreversible in a way nothing warns about.

### Watchlist editing is the one write in this API

Every other route reads. This one writes, and it is the exception worth making: the
watchlist decides what the capture job fetches, and a dashboard that can show a symbol,
say it has ten years of prices and no chains, and offer no way to fix that is a dead
end. The writes are small, reversible and idempotent; adding a symbol already present
reports `changed: false` rather than failing, because a double click is not an error.

### Two bugs found by looking at the rendered page

Both were in code written the same hour, and both were about **two vendors holding the
same symbol** — which only became possible earlier the same day.

- **The daily change was computed across vendors.** The obvious query ranks a symbol's
  rows by `session_date DESC` and takes the top two. With one source that is two
  consecutive days. With two sources it is *the same session from two vendors*, and the
  "change" is their disagreement rather than a market move. It rendered as **+0.00% on
  exactly AAPL and QQQ** while every single-sourced symbol showed a real move: the two
  richest histories in the database were the two showing nothing. A source is now chosen
  per symbol first, the one with the latest session, and both closes come from it.
- **Session counts were summed across vendors.** AAPL reported **5,699 sessions** of
  history, being 2,511 Alpaca plus 3,188 Market Chameleon over mostly the same dates.
  It has 3,188. `COUNT(DISTINCT session_date)` now.

Same root cause, opposite symptoms, and neither is visible in a unit test that seeds one
vendor. Both are pinned by tests that seed two.

A third was caught by the coherence validator added earlier the same day: the first
draft of the test fixture held `high` fixed while `close` climbed past it, and every bar
after the second was refused. The validator working, rather than the fixture being
awkward.

### What is deliberately not done

**Pinning does not warn about the selection effect yet.** Best plays reports the top
candidate across the watchlist, so pinning fifty symbols makes that a maximum over fifty
times more draws and the top of the list will look better with nothing having improved.
The UI now makes it one click to do that. The guard belongs in the ranking rather than
in the button, `calibration.py` already holds the cluster reasoning for this shape, and
it is the next thing to build.

---

## 2026-09-08: the dashboard on a phone

Two independent problems, both real, fixed separately.

### It could not be reached

`api_host` is `127.0.0.1`, so the server listened on loopback only and nothing else on
the network could connect regardless of how it rendered.

`optscan serve --lan` binds every interface, prints the address to open on a phone, and
prints the firewall rule to run. **The default stays loopback and a test pins that.**
This app has no authentication of any kind, so binding to the network puts the
positions, the journal and the imported broker P/L in front of everyone on it. That is
worth a deliberate flag rather than a config default somebody inherits.

Two details worth keeping:

- **The LAN address is found by opening a UDP socket toward a public address and
  reading back which interface the routing table chose.** Nothing is sent; a UDP
  connect is a purely local operation. `gethostbyname(gethostname())` was not used
  because on Windows it routinely returns 127.0.0.1 or a virtual adapter, and an
  address the phone cannot reach is worse than admitting there isn't one: it sends
  somebody off debugging a firewall over an address that was never going to work.
- **The firewall command is printed, never run.** It needs elevation and it changes a
  system security setting, so it belongs to the person at the keyboard. It is scoped to
  `-Profile Private -RemoteAddress LocalSubnet`: a rule on the public profile would
  follow a laptop onto cafe wifi, which is a much worse exposure than a home network.

### It did not render

The stylesheet had **two media queries and both were `prefers-reduced-motion`.** No
layout breakpoints at all. Measured at 375px: the fixed 224px sidebar took two thirds of
the width and never collapsed, and the content in what was left wrapped one word per
line, with the search box showing `Se`.

The sidebar becomes an **off-canvas drawer** below 860px rather than stacking above the
content. Stacking would push every screen down by the height of the nav, which on a
phone is most of the first viewport, and the nav is the thing needed least often.
`position: fixed` rather than absolute, so opening it halfway down a long table does not
put it above the viewport.

Every table in the app was already inside one of three wrappers, so the horizontal
scroll went on those. Deliberately **not** on `.panel`: it holds the search dropdown,
and a scroll container clips absolutely positioned children, which would cut the results
off at the panel edge.

`Panel`'s header moved from inline styles to a `.panel-head` class, because an inline
style cannot be reached by a media query and it had to stack.

Verified across all ten views at 375px: no horizontal page scroll and no element wider
than the viewport outside a scroll wrapper. The Browse table scrolls inside its wrapper,
622px of table in 331px of space, while the page itself stays put.

**One thing that looked like a bug and was not.** The drawer appeared frozen at
`translateX(-300px)` with the correct class applied and the correct rule matching, and
an inline `transform: none` did not move it either. The cause was the preview pane being
hidden: the page is not composited while it is not displayed, so CSS transitions never
advance and the computed value stays at the start of the transition. Removing the
transition made it snap correctly to `left: 0`. Worth recording because every symptom
pointed at a specificity problem in the cascade and none of it was.

---

## 2026-09-08: two servers, one port, and why the dashboard looked broken

Reported as a bug in the app: the Home page rendered
`Unexpected token '<', "<!doctype "... is not valid JSON`. There was no bug in the app.

**Two processes were listening on port 8000.** One started at 16:13 bound to
`127.0.0.1`, one started at 21:50 bound to `0.0.0.0`. Binding every interface
**succeeds** while another process holds loopback on the same port, because they are
different addresses and there is no conflict to report. uvicorn started cleanly and
said so.

Windows then routes to the most specific binding, so `localhost:8000` reached the older
process and `10.128.106.55:8000` reached the newer one. Measured at the same moment,
same URL path: `/api/home` returned HTML on localhost and JSON on the LAN address. The
old process predated the Home page by five hours, so its SPA catch-all answered a route
it had never heard of.

The symptom is the worst part. `/api/health` worked, because it exists in both. Only the
*new* endpoints failed, and they failed by returning the single page app's HTML, which
looks exactly like a router registration bug. Two of the three diagnostics run against
it were also misleading: introspecting `app.routes` showed no `/api` paths at all
because this FastAPI version wraps included routers as `_IncludedRouter` with
`path=None`, and the code was fine the whole time.

`optscan serve` now probes loopback before starting and prints what it means. Loopback
specifically, not the bind address: the question is whether something holds the binding
that `localhost` will reach, since that is the one that silently wins.

Not fatal. Running two ports deliberately is legitimate and refusing to start would be
worse than a sentence, so it is loud advice and a test pins that it does not exit.

### The same session, on phone access

`--lan` was added earlier the same day with a firewall rule scoped
`-Profile Private -RemoteAddress LocalSubnet`. Checking `Get-NetConnectionProfile`
afterwards found **both networks classified `Public`**, so the rule never applied and
the port was still closed. Checking the profile should have come before handing over the
command.

More importantly, the address turned out to be `10.128.106.55/24` on a **shared building
network**, not a home router. `LocalSubnet` there is up to 254 machines belonging to
strangers, and this app has no authentication of any kind.

**So the port stays shut on this machine.** The rule was removed rather than widened,
and `--lan` is the wrong tool on this network: with `0.0.0.0` bound, the only thing
between the positions and the building is a firewall profile classification, which is a
single setting away from wrong. The `--host` flag already covers the right answer: bind
the private-mesh interface only, and the shared network never sees the port at all.

The lesson worth keeping: **`--lan` is safe on a network you own and unsafe on one you
share, and nothing in the flag can tell the difference.** Its warning says there is no
login; it cannot say who else is on the subnet.

---

## 2026-09-08: charts for every symbol, and drilling into one day

Two complaints, both fair, and a third feature that arrived while fixing them.

### Every unpinned ticker was a dead page

Clicking AA from the catalogue gave `No stored snapshot for AA. Run optscan snapshot`
and nothing else, while **2,492 sessions of its daily bars sat in the database**. The
whole Underlying view was gated on a stored option snapshot, which was right when six
symbols had one and every one of them was pinned. After the bulk price sync it
describes six of 293.

The price panel now renders from `symbol` alone and every volatility panel below it is
conditional, with one amber note at the top saying what is missing and how to fix it.
The red error banner is suppressed on this view: it said the same thing more
alarmingly, immediately above the calmer sentence.

`price_history` reads **stored bars first** and falls back to the provider. That is
faster, works with no provider configured, and is the only reason a chart opens for a
symbol nothing has ever captured.

### There were no intraday candles

The chart's 1D/1W/1M were aggregation intervals over daily bars, not intraday. Alpaca
serves minute bars back to 2016-01-04, so `get_intraday_bars` was added outside the
`MarketDataProvider` interface, alongside the bulk daily fetch and for the same reason.

**Which vendor serves a candle is deliberately not `OPTSCAN_PROVIDER`.** That setting
chooses what captures option chains, and it is load bearing for reasons unrelated to
charting: an IV history belongs to one vendor, so switching it restarts every rank from
zero. A minute candle carries none of that history. `get_intraday_provider` is a
separate, narrow factory: prices only, never volatility, nothing stored.

Nothing intraday is cached to disk. A few hundred symbols at a decade of minute bars is
hundreds of millions of rows for a chart somebody looks at for ten seconds.

### Click a candle, open that day

Clicking a daily candle offers `1m 2m 5m` for that session. The prompt renders inline
above the controls rather than as a modal, because it is a small question about the
thing already on screen and a dialog would cover the chart it is asking about.

The session request is bounded to the **calendar day**, not to market hours. The
extended session runs 08:00 to 24:00 UTC and clipping to 13:30-20:00 would silently
drop the pre and post market bars, which on a quiet name are often the whole reason
somebody opened a specific day.

### Three bugs found while building it

**Intraday bars keyed by date collapse.** `BarOut.time` was an ISO date, which
identifies a *session*, so all 78 five minute bars in a day carried the same value and
lightweight-charts drew one candle with the daily axis still on it. It looks like a
data problem and is not. Intraday now emits epoch seconds; a series is one form or the
other and never mixes.

**Inferring intraday from the timestamp is wrong on real data.** The first fix decided
the format by asking whether a bar had a time of day on it. **Alpaca stamps daily bars
at 04:00Z**, not midnight, so every symbol served from the provider rather than from
storage would have been mislabelled. Caught by an existing test, not a new one. The
interval belongs to the caller and is now passed.

**A standalone effect calling `applyOptions` blanked the page.** Setting the time axis
in its own `useEffect` gave it a lifecycle independent of the chart's, so it could fire
after the creation effect's cleanup had called `chart.remove()`. The library reports
that as `Value is null` and React unmounts the tree. Moved into the effect that sets
the data, which has already proved the chart is alive.

Also: the axis must set `timeVisible` on an intraday series, or every label on a one
day chart is the same date and the axis says nothing at all. And the degradation notice
("drawn as a line") advised switching to a weekly interval, which does not exist in
session mode, so it now names the controls that are actually on the screen.

### A test that reached the network

Writing the fallback test made the suite really call yfinance, logging
`$NOPE: possibly delisted`. The rule here is that tests never touch the network, and it
was being broken by a test that otherwise passed. The provider dependency is now
overridden in that file.

---

## 2026-09-08: capturing every symbol, and a cleanup sweep

### The capture itself needed almost no new code, and one real fix

`run_snapshot` already took `symbols` and `provider`, so capturing the universe is two
CLI flags: `--universe [GROUP...]` and `--provider`. That is the whole feature.

What did need fixing was the request count. The first run showed the shape:

    per expiry:  contract metadata + chain snapshots + an underlying quote
    per symbol:  1 + 16 x 3 = 50 requests

The repeated quote is the giveaway. `get_chain` fetches the underlying every time it is
called, so sixteen expiries meant sixteen identical quote requests. At 293 symbols that
is roughly **14,000 requests, over an hour**, and most of a day's rate limit budget.

`get_chains(symbol, expiries)` is now on the provider interface with a **default that
loops `get_chain`**, so every adapter has a working implementation the day it is
written and only one that cares about request count overrides it. Alpaca's override
uses the expiry **range** both option endpoints accept and fetches the quote once.

Measured after: **CCJ 5 requests, QQQ 10 requests for 16 expiries and 4,780 contracts.**
About six on average, so 293 symbols is roughly 1,760 requests and nine minutes.

Extracting `_parse_contract_rows` and `_contract` so the single expiry and range paths
share them was not tidiness: open interest is the only reason the two hosts are joined
at all, and two parsers would eventually disagree about it.

### The job is scheduled, and deliberately not installed by default

`capture` runs at 15:50 market time, five minutes after the watchlist snapshot. The
watchlist is what the screener scans and must not queue behind three hundred symbols;
this is history for its own sake and can wait.

`default_install=False`, for the same reason `manage` is excluded. It is the only job
here that cannot run without a specific vendor's credentials, and it is nine minutes
over three hundred symbols. Whether that history is worth keeping is a decision.

**Capturing is not screening.** The watchlist stays at six. Best plays reports the top
candidate across whatever it scans, so pointing it at 293 symbols makes that a maximum
over fifty times more draws and the list would look better with nothing having improved.
Capture is irreversible if skipped; ranking is a display choice that can be fixed later.
They are separable and are being kept separate on purpose.

### Test captures that had to be deleted

Building the batch fetch meant running `--force` on a Sunday night, which wrote **19
captures under session 2026-09-08**, a session that had not opened. They hold Friday's
marks under a Monday label, which is exactly what the force warning describes. The
parquet partitions and the manifest rows were removed. Nothing legitimate could exist
for that date: `describe_state` reported the NYSE session as "pre" throughout.

Worth recording because the mistake is easy to repeat: `--force` is for testing the
mechanics, and anything it writes should be deleted before it reaches an IV history.

### The cleanup sweep

**A duplicated helper.** `_last_captured` in the watchlist router and `_last_capture` in
the catalogue were the same function, written twice a few hours apart. Two answers to
"when was this last captured" is the kind of thing that drifts silently. Now
`catalogue.last_capture`, imported by the router.

**Names nothing referenced.** Four were added this session and never used:
`ALPACA_INDICATIVE_DELAY_MINUTES`, `storage.vendor.symbols_held`, and the
`return_close` and `put_call_volume_ratio` properties on `VendorDailyBar`. The
reasoning each carried survives elsewhere: the delay in the adapter docstring, the
dividend argument in the `adj_close` field comment, and the put/call structural offset
in the parser docstring and the entry above.

Three were pre-existing and equally dead: `analytics.levels.total_volume`,
`views.expiry_or_none`, and `jobs.validate.suggested_schedule`, the last of which took
a `settings` argument it immediately discarded.

**A comment that was false.** `HEADER_ALLOWED` and `HEADER_USED` in the Tradier adapter
sat under a comment saying they "are logged when a limit is actually hit". They were
never read anywhere. Removed rather than wired up: the available count is what the
limiter needs, and a constant nothing references is a claim nobody checks.

The full suite passing after every removal is what says they were dead rather than
merely uncalled from where the scanner looked.

**Also removed:** a stray empty `data/optscan.db` created by pointing a cleanup script
at the wrong filename. The real database is `data/optscan.sqlite`.

---

## 2026-09-08: looking for lines to cut, and finding a bug instead

Asked whether anything could be trimmed. Measured first: 25,406 lines of source, of
which **60% is code, 19% docstrings, 5% comments, 17% blank**. The prose is not fat.
It is where every one of the eleven traps is recorded, and it is the reason they have
not been hit twice. Nothing was cut from it.

Two real duplications turned up, and one of them was hiding a bug.

### Three adapters, three different answers about zero

`tradier._clean_float`, `yfinance._clean_float` and `alpaca._positive` were near
identical, and the small differences were the interesting part:

    tradier    zero preserved   "a zero bid is a real state"
    yfinance   zero preserved   "it is a real quote state"
    alpaca     zero to None     "zero is not a price"

The first convention in this project's CLAUDE.md is **"None means unknown, 0.0 means
the vendor said zero. Never collapse the two."** Two adapters followed it and one did
not, with nothing anywhere saying so.

The Alpaca docstring justified it: "Alpaca publishes 0 for a side with no quote."
**That is false, and it was measured rather than argued about.** On SPY 2026-09-18, of
642 contracts:

    genuine 0.00 bid with a real ask : 57
    both sides zero (no quote at all) : 0
    contracts with no quote object    : 0

So every zero bid Alpaca publishes is a real one sided market on a far out of the money
wing, and the adapter was discarding the bid on 9% of the chain. After the fix all 57
come back, every one with a real ask, and nothing became None.

`providers/parsing.py` now holds the two honest answers, and which one a field takes is
a decision rather than a style choice: `non_negative` for a quote, `positive` for a
derived value like an implied vol, where a zero is a solver that gave up rather than a
measurement. That distinction is exactly what Tradier's unusable `mid_iv` taught, so
both halves are now stated in one place instead of implied in three.

`whole` joins them for counts, where zero is also real.

### Four copies of a PowerShell invocation

`subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ...])`
appeared four times across `launcher.py` and `schedule.py`. The duplication mattered
less than the flags: `-NoProfile` stops a user's own profile running before the command
and `-NonInteractive` stops a scheduled task hanging forever on a prompt nobody can see.
A fifth caller written from memory would plausibly omit one.

`jobs/powershell.py` owns it, with `run`, `output` and `failure_message`. Verified after
the change that `desktop_dir()` still resolves the OneDrive redirected Desktop and that
`task_states` still reads a registered task, since both go through it.

### What was left alone, and why

**`cli.py` at 1,423 lines is the largest file and is not a problem.** pyproject already
ignores PLR0915 there with the reason: it is long because it is flat, one statement per
flag and one return per subcommand. Collapsing the argument parser into a data
structure would scatter the definition of the command line and trade readable length
for unreadable cleverness.

**The repeated `generate(self, analysis, expiry, config)` across the strategy classes
is an interface being implemented,** not duplication.

**Tests are 13,797 lines against 25,406 of source.** That ratio is the point of them.

### The honest arithmetic

    removed from existing files   123
    added to existing files        38
    net removed from old code      85
    two new shared modules        129
    ------------------------------------
    tree total              25,406 -> 25,450

**Consolidating made the codebase slightly larger.** That is the true answer to "can we
trim lines": not really, and chasing it would be the wrong goal here. The value taken
was a correctness fix on 9% of every Alpaca chain and two behaviours that can now only
be changed in one place. Neither shows up as a smaller number.

---

## 2026-09-08: the live hub works on Alpaca, and Tradier and Schwab are gone

### The live hub was disabled for a reason that no longer applies

It was switched off because "yfinance has no documented rate limit and throttles
silently, and being throttled would break the 15:45 snapshot job". **Alpaca publishes
200 requests a minute.** At the shipped settings the hub costs one quote plus one chain
per subscribed symbol per cycle, capped at four symbols, every fifteen seconds, and it
expires a feed after sixty seconds with no subscriber. That is about 32 requests a
minute at the worst, against a budget of 200, and only while somebody has the Chain tab
open.

Driven by hand against the live API rather than started as a thread, so a failure would
be a traceback instead of a status event nobody reads. It works: `idle` to `live`, a
full first cycle of **394 QQQ contracts** at version 1, correctly flagged
`realtime: false`, a second tick emitting nothing because the market was shut and no
contract moved, then a clean release and stop.

**It was reporting the delay as unknown.** `quote_delay_minutes` publishes a number
only when a vendor documents one, and it knew about Tradier's sandbox and nothing else.
Alpaca's indicative feed is documented at fifteen minutes and the plan refuses
consolidated stock data inside the same window, so the number was there to publish and
was not being published. Now it is, and the status event carries `delay_minutes: 15`.

Worth noting against the entry above this one: `ALPACA_INDICATIVE_DELAY_MINUTES` was
deleted as dead code earlier the same day, correctly, because nothing referenced it.
This is where it belonged. Removing an unused constant and then needing it an hour
later is the expected cost of that kind of sweep, not an argument against doing it.

**Still off by default.** Whether the dashboard should poll a vendor whenever a tab is
open is a decision, and the argument for it changed today rather than being settled.

### Tradier removed

Strictly dominated. It implemented four of the interface's methods against Alpaca's
nine, and in particular **it has no corporate calendar**, contrary to what was said
while planning this: `get_events` was never implemented and inherited the base
refusal. Its `mid_iv` was measured unusable in Phase 5. Its token dies around 2026-10
unless the account is funded, and funding it forces a key regeneration.

Removed: the adapter, its two test files, and the whole fixture directory. The
fixtures were the largest part at 2,655 lines, and their value was pinning that
adapter's refusals, so they leave with it at no loss.

Also removed from config: `TradierEnvironment`, `TRADIER_HOSTS`,
`SANDBOX_DELAY_MINUTES`, four settings and two properties.

**Every docstring that mentions Tradier stays.** They record lessons that outlive the
integration: why an IV history belongs to one vendor, why the live feed polls instead
of streaming, why a provider without a calendar weakens two position triggers, and why
a vendor's implied vol is stored and never used. Deleting the code does not make those
false.

### Schwab removed

A ghost. It was in the `ProviderName` literal, had two credential fields, a validator
entry and a line in `safe_summary`, and **no adapter existed anywhere**. Setting
`OPTSCAN_PROVIDER=schwab` raised "not implemented yet. Schwab arrives later". It was a
Phase 0 plan that Alpaca has since filled, and config that advertises a provider the
app cannot build is config that lies.

`snapshot_history_days` went with them: declared, never read by anything.

### What it cost and what it did not

    24 files changed, 92 insertions, 3,896 deletions
    source   25,450 -> 24,918
    tests    13,797 -> 13,191

`extra="ignore"` on the settings model means an existing `.env` still holding
`OPTSCAN_TRADIER_TOKEN` is simply ignored rather than rejected, so nothing had to be
edited on the machine.

Eight tests failed on the removal and none were deleted for it. Four were about
credentials and the provider registry and were retargeted at Alpaca. Two were about
publishing a documented delay, which is now Alpaca's indicative feed rather than
Tradier's sandbox, and they test the same behaviour against the vendor that still has
it. That is the useful shape: a test that only fails because its example vanished is
testing the example, and one worth keeping is testing the rule.

---

## 2026-09-08: closing the holes before the first real capture day

### Nothing was refreshing the daily bars

293 symbols were synced by hand once. There was no scheduled job, so tomorrow's bars
would never have arrived and the price history would have silently stopped moving,
which is invisible on a chart that still draws.

`prices` now runs at 16:05 market time, after the universe capture rather than
competing with it for the same rate limit. It asks for ten days rather than the full
decade: a re-import of a session already held is a no-op, so only the genuine gap
costs anything, and the whole 286 symbol refresh takes **7.3 seconds**.

`--provider` was added to `prices sync` for the same reason `snapshot` has one: the
bulk fetch needs a vendor that takes many symbols per request, and the configured
default cannot.

### A phantom ticker, hidden by a silent coercion

The first refresh reported eight symbols returning no bars. Seven were real
delistings, and the eighth was `TRUE`, which is not a ticker and was never in the file.

**YAML 1.1 reads a bare `ON` as the boolean `True`.** `- ON` for ON Semiconductor
arrived at the loader as `True`, and `str(True).upper()` is `"TRUE"`. So ON Semi had
been silently absent from every sync since the file was written, a phantom was
requested in its place, the vendor answered with nothing, and the daily report blamed
it on a delisting. The same trap takes `NO`, `OFF`, `YES`, `Y` and `N`.

Two fixes, and the second is the one that matters. The symbol is quoted in the file,
which fixes the data. **The loader now refuses a non-string rather than `str()`-ing
it**, which is what stops the next one being invisible. ON was backfilled with 2,510
sessions it never had.

Seven delisted tickers were commented out with their last session rather than deleted:
LTHM 2024-01-03, HAYN 2024-11-20, SQ 2025-01-17, X 2025-06-17, JNPR 2025-07-01, PLL
2025-08-29, MMC 2026-01-13. Their stored history is real and stays. Leaving them in the
request list would have printed eight failures every single day, and a warning that
fires daily is one nobody reads, which is the same argument the health job already makes
about crying wolf.

The refresh now reports **286 of 286** with no warning.

### The resolve job cannot run at the time it is scheduled

Diagnosed rather than assumed. `WakeToRun` is false and this machine's wake events are
around 11:30 local, while `resolve` is scheduled for 05:00 local. It has missed five
days, and `StartWhenAvailable` has only caught it up once.

Not changed here, because both fixes are the user's call about their own machine:

- **Enable `WakeToRun`** and the machine wakes at 05:00 to settle. Preserves the
  original design, which runs it before the open so every expiry it can see is
  finished and its close is published.
- **Move `resolve_time_local` to after the close.** It would then settle through
  *today's* expiry rather than yesterday's, which is strictly more current, and it
  lands in the window the machine is reliably awake. The risk is the daily bar not
  being published in the first minutes after the close, which the job already refuses
  correctly rather than mis-settling: a trading day with no bar yet is missing, not
  shut.

### The schedule as it now stands

    snapshot  15:45  watchlist chains          installed
    capture   15:50  293 symbol chains, ~9m    NOT installed
    prices    16:05  daily bar refresh, ~7s    NOT installed
    record    16:15  validation logging        installed
    backup    16:30                            installed
    resolve   08:00  settle expiries           installed, missing days

Both new jobs are `default_install=False` because neither can run without Alpaca
credentials. All four installed tasks were verified as `DisallowStartIfOnBatteries`
false and `StartWhenAvailable` true, so the trap recorded in the scheduled task notes
is not present here.

---

## 2026-09-08: the other charts, and eleven more indicators

### What "price chart quality" actually meant

The price chart's quality was never the library. It was a set of decisions already
recorded in its own file: hovered values in a **fixed header rather than a floating
tooltip**, because a tooltip covers the part of the curve being pointed at; a measured
width; horizontal gridlines and no axis rule; controls that say *why* they cannot draw.

Six charts sat below that bar, and all six were hand rolled SVG at **fixed viewBox
widths** of 700, 460, 460, 260, 900 and 720. A fixed viewBox scaled to fit does not
just letterbox: it scales the type with it, so the same 11px axis label rendered at
7px in a narrow panel and 16px in a wide one.

**They stay SVG, and that is the decision worth recording.** lightweight-charts is a
time series library. A payoff diagram is profit against underlying price, a smile is
implied vol against strike, a term structure is implied vol against days. Forcing those
into a time series library means lying to its axis, and the lie surfaces as a date
formatter on a strike axis. Only the equity curve is temporal, and it is not worth a
second charting library on its own.

So `chart/plot.jsx` is the price chart's *behaviour* in one place: a measured width, a
`<Plot>` frame with the same grid and axis treatment, and `<PlotReadout>` for the
header. `PayoffChart` and the two volatility charts moved onto it and each gained a
readout, which is most of the point: hovering a payoff diagram is asking "what do I
make if it finishes here", and until now the answer had to be estimated off the axis by
eye.

`charts.jsx` went from 396 lines to 86 and re-exports the moved components, so no view
had to change an import.

**Width is measured in a layout effect, not only by ResizeObserver.** The observer
alone leaves the first frame at the fallback, which is a visible jump on a narrow
panel, and reports nothing at all for a container that is not being laid out.

Two smaller repairs found on the way. `LevelsChart` carried five colour fallbacks like
`var(--bad, #c85f5f)` from **before** the Webull palette rebase, so a token that failed
to resolve would have repainted the chart in last year's scheme; `theme.js` already
documents why a duplicated palette drifts. And its width was a `900` default the view
never passed.

### Eleven indicators, and the lower pane the registry had been waiting for

The registry's own docstring said an oscillator would need "a second price scale, which
is the one extension this file cannot absorb on its own". That extension is here.

Added: EMA9, EMA21, VWAP, Bollinger, Keltner and Donchian on the price pane; RSI, MACD,
Stochastic, ATR and relative volume on a lower one. With the three moving averages that
is fourteen chips.

**Only one lower indicator runs at a time, and that is a correctness rule rather than a
simplification.** RSI is bounded 0 to 100. MACD is unbounded and centred on zero.
Sharing one axis puts one of them in a corner labelled with the other's scale. Enabling
a second replaces the first, which needs no explanation on screen because the previous
chip visibly turns off as the new one turns on. Overlays are unaffected and stack
freely.

The maths lives in `chart/compute.js`, separate from the registry that declares it, and
every function keeps the rule the moving average already kept: **emit a point only
where the full window exists.** A fourteen period RSI over six bars is a different
statistic wearing the same label.

Three details in there worth not rediscovering:

- **RSI and ATR use Wilder's recursive smoothing, not a rolling mean.** Substituting a
  simple mean is the common error that makes an RSI disagree with every other platform
  by a point or two, which is exactly the size of disagreement nobody investigates.
- **Keltner emits a point only where both the EMA and the ATR exist.** A centre line
  without a width is not a channel, and drawing one would be a bare EMA wearing the
  Keltner label.
- **VWAP and relative volume refuse a window containing a bar with unknown volume**
  rather than treating it as zero weight. Same null-is-not-zero rule as the storage
  layer.

Verified numerically rather than by looking at it: RSI was enabled in the browser and
its chip read **53.95**, against **53.95** from a reference implementation written
separately in the console over the same 126 bars, producing 112 values with no leading
ramp.

## 2026-09-08: algo-style alerts, and the twelfth instance

### The question a signal module has to answer first

Not "is this condition interesting" but "how often does it hold anyway". An alert that
fires often is not an alert, it is a log line that trains its reader to ignore the
channel, and a muted alerting tool is worse than none because it is still believed to
be working. That argument is already in `alerts.py`; what was missing was the
measurement behind it.

So nothing shipped on a guessed threshold. `analytics/signals.py` is six detectors, and
every cut in it was chosen against a dump of the raw quantities over **44,113
symbol-days**: 294 symbols, one session in five across three years, levels rebuilt from
a trailing two years at each sample with no lookahead. Recording the quantities rather
than the verdicts meant sweeping a threshold cost nothing and no cut was picked by the
same pass that measured it.

At the defaults the whole universe produces about **nine signals a day, two of them at
severity 3 or above**. That is a channel someone reads.

### The fixed threshold was the trap, and it was the familiar one

The obvious squeeze is "Bollinger width below four percent of price". Measured, that
detector fires on **0% of days for the tenth-percentile symbol and 12% for the
ninetieth**. It is not measuring compression, it is measuring whether the ticker is a
utility or a biotech — the same shape as every constant-offset instance before it,
wearing volatility as its costume.

The standard squeeze avoids it by construction: Bollinger bands entirely **inside**
Keltner channels compares the spread of closes to the same symbol's own average true
range, so the comparison normalises itself. The measured spread under that rule is 3%
to 16% around a median of 9% — still a spread, but a spread of behaviour rather than of
identity.

### A state is not an event, and per-day suppression cannot fix that

The first measurement had the squeeze firing 27.8 times a day, which made no sense for
a rare condition until the obvious thing surfaced: **a squeeze runs a median of 4
sessions and up to 41**. It was firing every day it held. The once-per-symbol-per-kind-
per-session key is right and could not help, because each of those days legitimately is
a different day.

Edge triggering — fire on the transition into compression, not while in it — cut it
6.0x, from 10.44% of symbol-days to 1.75%. Level breaks needed no such fix; a crossing
is already an event.

### The twelfth instance, found by running it

The first live run reported `TRUE` breaking a swing high. Its last stored session was
**2026-01-21**, eight months earlier.

Every signal here is defined on the most recent bar, so a symbol whose history stopped
updating reports its final session forever, and reports it as though it were today.
Eight universe symbols had stopped trading between 2024 and early 2026, and they
produced **five of the fifteen signals**: LTHM at 5.8x volume, TRUE at 15.6x, PLL at
5.5x. Those are not surges. They are the last day of trading before an acquisition
closed, which is the highest volume day a ticker ever has. A third of the output was
archaeology presented as news.

The guard compares a symbol's last session to **the newest session held anywhere in the
database**, not to today's date. The calendar does not know which weekdays were
holidays, and a wall-clock reference would declare the entire universe stale every
morning until the price sync landed. After the guard: 286 symbols scanned, 9 signals,
the 8 dead tickers named in the report rather than silently dropped.

### Rarity is a conjunction, and the conjunction was checked rather than assumed

The composites require two conditions at once — RSI at an extreme *and* price at a
swing level that beat its own significance test. The argument for that being rare is
that two independent conditions at 10% coincide at 1%, and the argument is worth
nothing if the halves are correlated.

Measured: the composites fire **1.4x and 1.6x** more often than independence predicts,
because the decline that carves a swing low is the same decline that depresses RSI. A
mild positive lift, not the tenfold one that would mean the two halves were the same
condition wearing different names. So the docstring says 1.4x rather than claiming
independence it does not have.

The related trap was avoided by omission: "oversold **and** at the lower Bollinger
band" is not offered, because RSI and band position are both functions of the same
recent closes. That is one condition counted twice.

### Only a level with a p-value may raise an alert

`Level.p_value` is None for the kinds where there is nothing to test — a round number
has no touch count, and a volume node's height is not a count of events. Value areas
are in that group, which turned out to settle a design question rather than merely
exclude a case: `value_area_low` and `value_area_high` share one `LevelKind`, so a
level alert could not tell a floor from a ceiling even if it wanted to. Support means a
swing low that beat chance; resistance means a swing high that did.

An earlier draft had value areas in the support and resistance predicates, which was
dead code advertising a capability it did not have.

### Two smaller decisions

**A level that broke does not also report as approached.** Price is necessarily near a
line it just closed through, so reporting both is one event told twice — and the
composite goes with it, because closing *through* support is a breakdown, not a bounce,
and "oversold at support" would invert what happened. A test pins this; it was found by
a test failing on a fixture whose level sat exactly on the last close.

**The dashboard route evaluates and delivers nothing.** A page refresh that consumed
the once-per-session suppression would leave the scheduled run silent — the worst
failure this kind of tool has, because it still looks like it is working.

### Plumbing

`Alert` now carries either a position `Trigger` or a market `Signal`, with
`position_id: int | None`. None rather than a sentinel zero, so a reader of
`alerts.jsonl` can tell "no position" from "position 0" and no foreign key points at
nothing; the key is omitted from the payload entirely for a market alert, so existing
rows keep exactly the shape they had. Suppression lives in a new `signal_sent` table
keyed `(symbol, kind, session)` — migration 9 — because a break in March and another in
July are two events rather than a repeat.

`ema` and `keltner_channels` were added to `analytics/levels.py`, seeded the same way
the frontend seeds them. Verified against the JavaScript the chart actually draws:
EMA, ATR, Keltner and RSI agree to the last bit, Bollinger to 1e-12 (the JS carries a
rolling sum of squares, Python re-sums the window). The chart and the alert cannot
disagree about where a band sits, which matters when one draws the line and the other
sends mail about it.

1536 tests pass. The new modules are at 90% with the router at 100%; the aggregate
moved 83 to 84.

## 2026-09-08: a backtester, and four ways it tried to lie

### The data decided the scope, not the request

The ask was a backtesting tab for strategies. What is actually testable came out of one
query: 294 symbols and ten years of daily bars, historical implied volatility for **two**
symbols, and nine stored option chain captures.

Nine captures cannot backtest anything. So underlying rules run on the full universe with
real data, and the option modes run on QQQ and AAPL and **refuse everywhere else**. The
refusal is a feature and it is enforced in code: the obvious workaround is to substitute
realized volatility for implied, and that is not a degraded approximation, it is the
removal of the quantity being measured. The implied-to-realized gap *is* the variance
risk premium; pricing entry at realized vol prices it at fair value, where expected
profit is zero by construction. A backtest built that way reports no edge for a strategy
that has one, and the next move is always to fudge it. `require_implied_vol` raises.

What *can* be done honestly is better than it sounds. Held to expiry, a short option's
profit is `credit - intrinsic at expiry`, and the second term is exact because the
underlying's path is real stored history. Only the fill is modelled.

### Four ways a backtest lies, and the guard for each

**It compares against zero.** A long rule over the last decade made money because the
market rose. Every number is measured against a null instead.

**It counts overlapping trades as independent.** The unit is a block, not a row.

**It is the best of many tries.** `sweep` reports the winner against the best-of-N under
the same null.

**It trades on information it did not have.** Entries fill at the **next bar's open**. A
rule reading today's close cannot be filled at today's close, and for any close-based
rule that delay is most of the difference between a backtest and a fantasy.

Two smaller ones worth recording. A bar that hits both the target and the stop is booked
as the **stop**, because the intrabar order is unknowable and assuming the good one is a
few basis points a trade that never existed. And a trade that has not finished inside the
stored history is **dropped** rather than marked to market, because keeping it fills the
most recent weeks with truncated winners, which is exactly the period a reader studies
hardest.

### The null was wrong the first time, and the way it was wrong is the interesting part

The first null drew fresh random entry days per symbol, matched on count and horizon. It
looked principled and it is far too generous on a correlated universe.

A rule that fires across ninety-eight technology names in the same week produces a mean
with the variance of roughly **one** observation, because those names share almost all of
their variance. A null that scatters each symbol's entries independently averages that
co-movement away and produces a mean with the variance of ninety-eight. Comparing a noisy
number against a stable one clears the bar almost regardless of merit.

The replacement is a **circular shift of the whole entry pattern**: every symbol's entries
move by the same number of bars, wrapping at the end. That preserves the trade count per
symbol, the clustering in time and the synchronisation across symbols, and destroys only
the alignment between the rule and what the market did next — which is the thing on trial
and the only thing that should be destroyed.

`effective_sample` had the same shape of error. It keyed on (symbol, block), which sounds
more careful and measures nothing: with overlapping entries suppressed, consecutive
trades on one symbol are already at least a horizon apart, so each lands in its own block
and the count comes back equal to the number of trades. On a real run it reported 9,606
independent observations from 9,606 trades. Pooling by calendar block across symbols gives
106, and 106 is the number that belongs next to a claim.

### The +2,737,313 percent year

The first per-year table reported 2025 at +2,737,313%. The equity curve multiplied every
trade's return together, which treats them as a sequence of bets each staking the whole
account. They are not a sequence: a rule firing across ninety-eight symbols opens
ninety-eight concurrent positions, and compounding those fabricates leverage of about
ninety-eight to one. The absurd number was the giveaway; a subtler version of the same
error at three symbols would have passed for a good year.

An equity curve needs a capital model, so there is one now and it is stated rather than
implied: hold every trade exiting in a month at equal weight, compound the months. One
point per month, not per trade.

### What it says about this tool's own signals

The honest part of building this was pointing it at the signals shipped the same day.

None of the indicator rules beat the null on the tech group at a 21 day horizon.
`rsi_below` came closest at +3.26% against a null of +2.45%, a margin of +0.79% inside a
null whose 5th-to-95th range ran from +0.86% to +4.59%, p=0.244. The squeeze and the
overbought rules came out slightly negative. `every_bar` returned an edge of exactly
+0.000%, which is the sanity check working.

Selling 5% out of the money 30 day puts on QQQ and AAPL won **88%** of the time for
**+0.12% of collateral** per trade, and went **negative at 15% slippage**. That last line
is the most useful output the tool produced all day: the entire margin of the strategy
sits inside plausible transaction costs.

And a sweep of seven RSI thresholds picked 20 as the winner at +9.23%, against +4.69% for
the best of seven cells under the null — on **five trades in four independent blocks**.
The report now says that in the note, because a tighter parameter selects a rarer
condition, so the winning cell is very nearly always the one with the least evidence
behind it.

None of that is a reason to stop. It is a reason to have built the null first.

### Plumbing

`rules.py` holds rolling indicators because calling `levels.py`'s scalar forms once per
bar is quadratic: 2,500 sessions across 294 symbols is about four billion operations for
a single pass's worth of question. That makes a second implementation of the same
arithmetic, which is a standing hazard rather than a tidy separation, so
`tests/test_rules.py` pins every one against its scalar twin at several points in the
series — the same check already run between `levels.py` and the frontend's `compute.js`.

`optscan backtest --json` exists so an agent can run one and read the result.

1,608 tests pass. The new modules are at 89%.

## 2026-09-08: refining the edge search, and the first thing that survived

### A hypothesis, measured and thrown away

The plan was to measure returns relative to the contemporaneous universe, on the
reasoning that the market factor dominates a single trade's variance and does not average
away across trades, so removing it should make small edges visible.

Measured, it does shrink the null's spread: `rsi_below`'s null standard deviation fell
from 1.97% to 1.31%. It also shrinks the observed mean by the same factor, so the z-score
went from 0.57 to **0.32** — worse. The reason is that the shift null was already doing
the job: it slides the entry pattern to a different point in history, so both the
strategy and the null experience the market's drift, and the comparison between them is
already market-adjusted. Explicit neutralisation was redundant with machinery that
existed.

It is recorded here so nobody rebuilds it. The exploratory script that produced the first
version of this measurement also allowed overlapping trades and reported `volume_surge`
at p=0.040, where the production backtester with overlap suppressed says p=0.267. The
same trap, in the tool built to avoid it, in a script written by the person who built it.

### The thirteenth instance: ranking the horizon instead of the rule

The first grid search over 78 combinations returned a top twelve in which **every single
candidate was a 63 day hold**. Not one five day or twenty one day rule appeared. Three
months of market drift returns six to seven percent whatever triggered the entry, so
sorting candidates by mean return sorts them by holding period and the rules are noise on
top.

The fix is the same shape as every other instance in this repo: normalise before
comparing. Each candidate is now scored against **its own null**, and ranked by
`(mean - null_mean) / null_spread`. Doing that changed the answer completely. The top of
the list stopped being long holds and became short-horizon mean reversion, and the second
place candidate became a *short* — a direction that had not appeared at all under the
old ranking.

Making that affordable needed one optimisation. A forward return table without a target
or a stop has a known exit bar, so it collapses from O(horizon) per bar to one division;
and the table depends only on the horizon and direction, not the rule, so six tables
serve seventy-eight candidates. The search runs in thirteen seconds.

### The multiplicity bar, in the right units

`expected_best_under_null` originally computed a return, from a quantity that was the
finalist's edge rather than the null's spread. Wrong, and awkward to interpret. It is now
a z-score: the best of n candidates reaches about `sqrt(2 ln n)` under the null, which is
2.95 for a 78 cell grid. The tech search's winner scored 2.39, so the in-sample ranking
settles nothing, which is exactly what the holdout is for.

### The first thing that survived

Search on the tech group, holdout from 2023-05-17. Three finalists went to the holdout;
`rsi_below(25) long 5d` returned +2.19% at p=0.034, and thresholds 25, 30 and 35 all
appeared in the top four by z. A family of related parameterisations ranking together is
worth more than any single p-value, so it was worth one confirmation.

The confirmation was pre-specified and run on the **188 symbols outside the tech group**,
which the search never saw:

    RSI<25, 5 day hold    +0.98% over null   p=0.005   107 blocks
    RSI<30, 5 day hold    +0.71% over null   p=0.004   118 blocks
    RSI<30, 21 day hold   +1.21% over null   p=0.017   117 blocks

Four tests, three of them at p below 0.02, on a cross-section chosen before the numbers
were seen, against a null that already contains the drift and preserves the entries'
clustering and co-movement.

That is the first result in this project that has survived everything thrown at it. What
it is not: it is one market, one decade, one universe that is survivorship biased by
construction, and an effect of roughly 0.7 to 1.0 percent per trade against an assumed
ten basis points of cost. At five day holds that is a lot of trading for a thin margin,
and the cost assumption has not been checked against what these names actually cost to
trade. The next thing to do is measure that, not to trade it.

1,620 tests pass.

## 2026-09-08: what it costs, what actually works, and one idea worth listing

### The placeholder was wrong by five times

Every backtest here had been charging ten basis points a round trip. That was a guess,
and it was load bearing: the strongest result so far is worth seventy to a hundred basis
points a trade, so the guess decided whether the finding existed.

No historical quotes are stored and fetching a decade of NBBO for three hundred symbols
to answer one question is the wrong trade, so `analytics/costs.py` estimates the spread
from the bars themselves. Corwin and Schultz start from the observation that a daily high
is probably a buy at the ask and the low a sell at the bid, so a one day range holds one
spread and one day of volatility while a two day range holds one spread and two days of
volatility. Volatility scales with time and the spread does not, so they separate.

Measured across the 188 symbols outside the tech group: **median 47 basis points, mean
61**, ETFs at 30, metals at 61, uranium at **153**. Not ten.

Two things went wrong on the way. The first `trustworthy` gate rejected any symbol whose
daily estimates came out negative more than 35% of the time, and threw away all 188:
negative daily values are a normal feature of this estimator rather than a fault, and the
*tightest* names are the most negative because a small spread is the hardest to resolve.
The gate now only checks sample size. The second was a test fixture with a constant close
and a fixed 2% daily range, asserting the estimate should be zero. It is 2%, correctly:
with no overnight movement the entire intraday range **is** bid-ask bounce. A fixture
without volatility cannot exercise a separation of volatility from spread.

### Costs do not change an edge, only the take-home

Re-running the pre-specified reversion test with per-symbol costs gave an identical
`edge` (+0.71%) and a lower `mean_return` (+0.93% to +0.46%). That is right and worth
stating plainly: the null pays what the strategy pays, so cost cancels out of a timing
comparison. The edge says whether the rule works. The mean says whether you keep anything.

The surprise was where it works best. On the ETF and large cap subset, where spreads run
30 to 47 basis points, the same rule nets **+1.03% at p=0.001**. Better than the full
universe, because the effect is roughly constant while the cost is not. That inverts the
usual worry about anomalies living where they cannot be traded.

### A 184 cell search, and the most useful failure yet

Seven documented patterns went into the registry: internal bar strength, consecutive
up and down closes, gap down, 52-week-high proximity, turn of month, and the two IBS
directions. IBS is the best documented of them, with evidence on equity ETFs running from
the 1990s to the present and a noted weakening since about 2013 that a 2023 to 2026
holdout is well placed to test.

The search ran 184 combinations over 123 liquid symbols with real per-symbol costs. Its
winner was `gap_down(0.04) short 1d` at **z = 7.70** against a multiplicity bar of 3.23,
on 76 independent blocks. Everything about it looked like a discovery.

Out of sample it returned **-0.34%**. Its two sibling cells returned -1.70% and -1.23%.
All three reversed sign. Shorting gap downs worked while gaps continued and stopped when
they began to fill, somewhere around the training boundary.

That is the single most useful result of the day. The multiplicity bar was cleared by
more than double and the finding was still nothing, which is exactly the case a holdout
exists to catch and exactly the case that no in-sample statistic can.

### Trade ideas, and the rule about them

`jobs/ideas.py` lists what is triggering today, and it can only list strategies that have
survived a documented test. `ValidatedStrategy` cannot be constructed without `Evidence`,
which records the population tested on, the block count, the p-value, the net return and
the cost basis, and the CLI prints that evidence underneath the tickers every time rather
than on request. A screen of symbols with no numbers beside it is the artefact the module
exists to avoid producing.

The registry has one SUPPORTED entry, `oversold_bounce`, and one RETIRED entry,
`gap_down_continuation`, kept with the reason. A strategy that stops working is not
deleted: the record of what was believed and why is the only thing that makes the next
search less credulous than the last.

Ideas are ordered by trigger age and then by the symbol's own spread, because the edge is
a fixed number and the cost is not, so the same signal is worth materially less on a thin
name. Stale symbols are excluded here even though the backtester keeps them: a delisted
ticker's final session triggers forever and would sit at the top of a list of things to
trade today.

1,639 tests pass.
