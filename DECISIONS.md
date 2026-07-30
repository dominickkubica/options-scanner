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
