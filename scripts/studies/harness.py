"""Shared plumbing for pre-registered studies.

Every study here does the same five things around its hypotheses: restrict history to a
development or holdout window, register its entry rules with the backtester, run a
strategy through `job.run` so it gets the real circular-shift null, summarise the result,
and apply a gate fixed before anything ran. Written once here so a new study is its
hypotheses and nothing else, and so the five things cannot drift between studies.

    from harness import DEV, HOLDOUT, Study, oversold

    study = Study("my_study")
    study.rule("base_oversold", oversold)
    with study.window(DEV):
        rows = [study.run("cds", entry="base_oversold", mode="call_debit_spread", ...)]
    study.show(rows, study.bh(rows))
    study.save({"dev": rows})

The four null studies of 2026-10-21 (`vrp_study`, `vrp_delta_study`, `bounce_study`,
`switch_study`) predate this and are kept as written, as the record of what ran.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import optscan.jobs.backtest as job
from optscan.analytics import rules
from optscan.analytics.premium_stats import benjamini_hochberg
from optscan.config import get_settings

DEV = (date(2014, 1, 1), date(2021, 12, 31))
#: A month after DEV ends, so no development trade settles inside the holdout.
HOLDOUT = (date(2022, 2, 1), date(2030, 1, 1))
FULL = (date(2014, 1, 1), date(2030, 1, 1))

#: The 23 symbols with stored IV30 and enough history for the option modes.
IV_SYMBOLS = [
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "BE",
    "GLD",
    "GOOG",
    "IWM",
    "META",
    "MRNA",
    "MSFT",
    "MU",
    "NFLX",
    "NVDA",
    "PLTR",
    "QQQ",
    "RCL",
    "SPOT",
    "SPY",
    "SYK",
    "TJX",
    "UNH",
    "WFC",
]
#: The ten most liquid of them, where fills are closest to mid.
LIQUID = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOG", "AMD"]

RESULTS = Path(__file__).parent / "results"

#: Below this many bars inside a window a symbol is dropped rather than half-tested.
MIN_WINDOW_BARS = 260


def oversold(bars):
    """The validated `oversold_bounce` trigger: RSI(14) at or below 30, as a state."""
    series = rules.rsi_series(bars, 14)
    return [i for i in range(rules.WARMUP, len(bars)) if series[i] is not None and series[i] <= 30]


class Study:
    """One study's runs, sharing a settings object, a window and a results file."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.settings = get_settings()
        self._original_load = job.load_series
        self._range = FULL
        job.load_series = self._windowed

    def _windowed(self, settings_, symbols):
        series, skipped = self._original_load(settings_, symbols)
        lo, hi = self._range
        kept = {}
        for symbol, bars in series.items():
            inside = [bar for bar in bars if lo <= bar.ts.date() <= hi]
            if len(inside) > MIN_WINDOW_BARS:
                kept[symbol] = inside
        return kept, skipped

    def full_history(self, symbols: Sequence[str]):
        """Unwindowed bars, for building conditions that must see before the window."""
        return self._original_load(self.settings, list(symbols))[0]

    @contextmanager
    def window(self, span: tuple[date, date]) -> Iterator[None]:
        previous, self._range = self._range, span
        try:
            yield
        finally:
            self._range = previous

    @staticmethod
    def rule(name: str, fn: Callable) -> None:
        """Register an entry rule for this process. Never written to the real registry."""
        rules.REGISTRY[name] = {"fn": fn, "params": {}, "label": name}

    def run(self, label: str, **strategy) -> dict:
        """One `job.run`, summarised. Defaults: 300 null draws, seed 0, so it repeats."""
        strategy.setdefault("draws", 300)
        result = job.run(self.settings, job.Strategy(name=label, **strategy))
        if result.stats is None or result.edge is None:
            return {"label": label, "trades": 0}
        stats, edge = result.stats, result.edge
        return {
            "label": label,
            "trades": stats.trades,
            "blocks": stats.effective_sample,
            "mean": stats.mean_return,
            "median": stats.median_return,
            "win": stats.win_rate,
            "null": edge.null_mean,
            "edge": edge.edge,
            "p": edge.p_value,
            "years": [(y.year, y.trades, y.mean_return) for y in result.by_year],
        }

    @staticmethod
    def bh(rows: Sequence[dict]) -> list[float | None]:
        return benjamini_hochberg([row.get("p") for row in rows])

    @staticmethod
    def gate(rows: Sequence[dict], qs: Sequence[float | None], *, q_max=0.10, beat=0.0):
        """The rows that pass a pre-registered bar: q below `q_max`, edge above `beat`."""
        return [
            row
            for row, q in zip(rows, qs, strict=True)
            if q is not None and q < q_max and row.get("edge", float("-inf")) > beat
        ]

    @staticmethod
    def show(rows: Sequence[dict], qs: Sequence[float | None] | None = None) -> None:
        for index, row in enumerate(rows):
            if not row.get("trades"):
                print(f"  {row['label']:32} no trades", flush=True)
                continue
            q = "" if qs is None or qs[index] is None else f" q {qs[index]:.3f}"
            print(
                f"  {row['label']:32} n {row['trades']:5} blk {row['blocks']:4} "
                f"mean {row['mean'] * 100:+7.2f}% med {row['median'] * 100:+7.2f}% "
                f"null {row['null'] * 100:+7.2f}% edge {row['edge'] * 100:+7.2f}% "
                f"p {row['p']:.3f}{q} win {row['win'] * 100:3.0f}%",
                flush=True,
            )

    def save(self, data: dict) -> Path:
        RESULTS.mkdir(exist_ok=True)
        path = RESULTS / f"{self.name}.json"
        path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return path
