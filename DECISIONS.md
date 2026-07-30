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
