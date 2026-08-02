"""Fail if any analytics or screener module falls below the coverage floor.

Run after the suite, against the data it left behind:

    venv\\Scripts\\python -m pytest --cov=src/optscan
    venv\\Scripts\\python scripts/coverage_floor.py

## Why this is per module rather than a total

`fail_under` compares one number for the whole package, and a package total is exactly
the shape of measurement this project keeps getting caught by: it is dominated by
structure rather than by the thing being asked about. When this check was written,
analytics and screener together reported 90 percent while `screener/positions.py`, the
module that decides what a held position is worth, sat at 18. The total was not wrong.
It was answering a different question than the one anybody cared about.

So the floor applies to every file separately, and the aggregate is printed but never
gates anything.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import coverage

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Packages the Phase 9 roadmap names. Everything else is measured but not gated.
GATED = ("src/optscan/analytics", "src/optscan/screener")

FLOOR = 80.0


def gated_files(report: dict) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for name, entry in report["files"].items():
        relative = Path(name).as_posix()
        if not any(part in relative for part in GATED):
            continue
        rows.append((relative, entry["summary"]["percent_covered"]))
    return sorted(rows, key=lambda row: row[1])


def main() -> int:
    data_file = REPO_ROOT / ".coverage"
    if not data_file.exists():
        print(f"No coverage data at {data_file}. Run pytest --cov=src/optscan first.")
        return 2

    cov = coverage.Coverage(data_file=str(data_file))
    cov.load()
    with tempfile.NamedTemporaryFile("r+", suffix=".json", delete=False) as handle:
        cov.json_report(outfile=handle.name)
        report = json.loads(Path(handle.name).read_text(encoding="utf-8"))
    Path(handle.name).unlink(missing_ok=True)

    rows = gated_files(report)
    if not rows:
        print("No analytics or screener files were measured. That is not a pass.")
        return 2

    failures = [(name, percent) for name, percent in rows if percent < FLOOR]

    print(f"{len(rows)} gated modules, floor {FLOOR:.0f} percent")
    print(f"  lowest    {rows[0][0]} at {rows[0][1]:.0f}")
    print(f"  aggregate {report['totals']['percent_covered']:.0f} percent over everything measured")

    if failures:
        print(f"\n{len(failures)} below the floor:")
        for name, percent in failures:
            print(f"  {percent:5.1f}  {name}")
        return 1

    print("\nEvery gated module is above the floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
