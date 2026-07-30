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
