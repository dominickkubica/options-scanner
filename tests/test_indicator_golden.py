"""The chart's indicator maths against the Python the alerts and backtests use.

`compute.js` draws RSI, the EMA, ATR and the Bollinger and Keltner bands on the chart,
`levels.py` computes the same indicators for alerts, and test_rules.py pins `rules.py`
to `levels.py`. Until 2026-09-12 the JavaScript third was pinned only by a sentence
(CHORE-0001). Now:

- `indicator_golden.json` holds `levels.py`'s values on the real SPY fixture at points
  spread through the series, including the first bar every indicator exists on, which
  is where seeding and alignment bugs live. The first test fails if the file and
  `levels.py` ever disagree.
- `frontend/tests/compute.test.js` checks `compute.js` against the same file, and the
  second test runs it, so the pre-commit suite fails when the chart drifts.

After a deliberate change to the maths, regenerate the file with
`OPTSCAN_WRITE_GOLDEN=1 pytest tests/test_indicator_golden.py` and review its diff.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from optscan.analytics import levels
from optscan.models import PriceBar

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "spy_daily_bars.json"
GOLDEN = ROOT / "tests" / "fixtures" / "indicator_golden.json"
FRONTEND = ROOT / "frontend"
JS_TEST = FRONTEND / "tests" / "compute.test.js"

#: Bar 20 is the first on which a 20 period Keltner channel exists (its ATR needs 21
#: bars), so it and the bar after are where an off by one would show.
CHECKPOINTS = (20, 21, 40, 60, 100, 250, 500, 900, 1099)


def load_bars() -> list[PriceBar]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fetched = datetime(2020, 1, 2, tzinfo=UTC)
    return [
        PriceBar(
            symbol=payload["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            fetched_at=fetched,
            source="fixture",
        )
        for row in payload["bars"]
    ]


def compute_golden(bars: list[PriceBar]) -> dict:
    """`levels.py`'s values at every checkpoint, each over the bars up to it only."""
    points = []
    for index in CHECKPOINTS:
        window = bars[: index + 1]
        bands = levels.bollinger_bands(window, 20, 2.0)
        channels = levels.keltner_channels(window, 20, 1.5)
        assert bands is not None and channels is not None
        points.append(
            {
                "index": index,
                "ts": window[-1].ts.isoformat(),
                "rsi14": levels.rsi(window, 14),
                "ema20": levels.ema(window, 20),
                "atr14": levels.atr(window, 14),
                "bb_middle": bands.middle,
                "bb_upper": bands.upper,
                "bb_lower": bands.lower,
                "kc_middle": channels.middle,
                "kc_upper": channels.upper,
                "kc_lower": channels.lower,
            }
        )
    return {
        "fixture": "tests/fixtures/spy_daily_bars.json",
        "written_by": "tests/test_indicator_golden.py",
        "points": points,
    }


def test_the_golden_file_is_what_levels_py_computes() -> None:
    expected = compute_golden(load_bars())
    if os.environ.get("OPTSCAN_WRITE_GOLDEN"):
        GOLDEN.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert [point["index"] for point in golden["points"]] == list(CHECKPOINTS)
    for stored, fresh in zip(golden["points"], expected["points"], strict=True):
        assert stored["ts"] == fresh["ts"]
        numbers = {key: value for key, value in fresh.items() if isinstance(value, float)}
        assert {key: stored[key] for key in numbers} == pytest.approx(numbers)


def test_compute_js_agrees_with_the_golden_file() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so compute.js cannot be run here")
    result = subprocess.run(
        [node, "--test", str(JS_TEST)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
