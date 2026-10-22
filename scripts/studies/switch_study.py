"""Do several indicators agreeing on a turn pick better periods to hold a stock?

Buying and holding is not tested here because it is the null: random entry, held N
days, is exactly what the backtester compares every rule against. The question is
whether a confluence of signals marks the start of a move better than chance.

PRE-SPECIFIED, before anything ran:

  bull_switch   close crosses above its 50 day mean today (below it yesterday), RSI(14)
                above 50, and volume at least 1.5x its own 20 day average. Long.
  bear_switch   the mirror: crosses below the 50 day mean, RSI below 50, volume 1.5x.
                Short, because the option version of this is a put.

each at horizons of 10 and 21 sessions. Four tests, BH across them, every symbol's own
estimated cost. DEV <= 2021-12-31, HOLDOUT >= 2022-02-01, and a cell reaches the
holdout only with dev q < 0.10 and a positive dev edge.

For anything that survives, the report also prints how big the moves are against what
an at-the-money option of that tenor costs, since a directional edge only pays through
an option if the move clears the premium.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import optscan.jobs.backtest as job
from optscan.analytics import rules
from optscan.analytics.premium_stats import benjamini_hochberg
from optscan.config import get_settings

OUT = Path(__file__).parent / "results" / ("switch_study_results.json")
DEV = (date(2014, 1, 1), date(2021, 12, 31))
HOLDOUT = (date(2022, 2, 1), date(2030, 1, 1))
settings = get_settings()
_original_load = job.load_series
WINDOW = {"range": DEV}


def windowed_load(settings_, symbols):
    series, skipped = _original_load(settings_, symbols)
    lo, hi = WINDOW["range"]
    kept = {}
    for symbol, bars in series.items():
        inside = [bar for bar in bars if lo <= bar.ts.date() <= hi]
        if len(inside) > 260:
            kept[symbol] = inside
    return kept, skipped


job.load_series = windowed_load


def switch(direction: str):
    def fn(bars):
        ma = rules.moving_average_series(bars, 50)
        rsi = rules.rsi_series(bars, 14)
        vol = rules.volume_ratio_series(bars, 20)
        out = []
        for i in range(max(rules.WARMUP, 51), len(bars)):
            if None in (ma[i], ma[i - 1], rsi[i], vol[i]):
                continue
            above_now = bars[i].close > ma[i]
            above_before = bars[i - 1].close > ma[i - 1]
            if direction == "up":
                hit = above_now and not above_before and rsi[i] > 50
            else:
                hit = (not above_now) and above_before and rsi[i] < 50
            if hit and vol[i] >= 1.5:
                out.append(i)
        return out

    return fn


rules.REGISTRY["bull_switch"] = {"fn": switch("up"), "params": {}, "label": "bull"}
rules.REGISTRY["bear_switch"] = {"fn": switch("down"), "params": {}, "label": "bear"}

CELLS = [
    ("bull_switch", "long", 10),
    ("bull_switch", "long", 21),
    ("bear_switch", "short", 10),
    ("bear_switch", "short", 21),
]


def one(entry, direction, horizon):
    strategy = job.Strategy(
        name=f"{entry}/{horizon}",
        entry=entry,
        mode="underlying",
        direction=direction,
        horizon=horizon,
        cost_model="estimated",
        draws=300,
    )
    result = job.run(settings, strategy)
    if result.stats is None or result.edge is None:
        return {"entry": entry, "direction": direction, "horizon": horizon, "trades": 0}
    moves = sorted(abs(t.net_return) for t in result.trades)
    return {
        "entry": entry,
        "direction": direction,
        "horizon": horizon,
        "trades": result.stats.trades,
        "blocks": result.stats.effective_sample,
        "mean": result.stats.mean_return,
        "win": result.stats.win_rate,
        "null": result.edge.null_mean,
        "edge": result.edge.edge,
        "p": result.edge.p_value,
        "median_abs_move": moves[len(moves) // 2] if moves else None,
    }


def show(rows, qs=None):
    for index, row in enumerate(rows):
        if not row.get("trades"):
            print(f"  {row['entry']:12} h{row['horizon']:<3} no trades", flush=True)
            continue
        q = "" if qs is None or qs[index] is None else f" q {qs[index]:.3f}"
        print(
            f"  {row['entry']:12} h{row['horizon']:<3} n {row['trades']:6} blk {row['blocks']:4} "
            f"mean {row['mean'] * 100:+6.3f}% null {row['null'] * 100:+6.3f}% "
            f"edge {row['edge'] * 100:+6.3f}% p {row['p']:.3f}{q} win {row['win'] * 100:3.0f}% "
            f"|move| med {row['median_abs_move'] * 100:4.1f}%",
            flush=True,
        )


def main():
    WINDOW["range"] = DEV
    dev = [one(*cell) for cell in CELLS]
    qs = benjamini_hochberg([row.get("p") for row in dev])
    print("DEV 2014-2021 (BH across these four)", flush=True)
    show(dev, qs)
    chosen = [
        row
        for row, q in zip(dev, qs, strict=True)
        if q is not None and q < 0.10 and row.get("edge", -1) > 0
    ]
    WINDOW["range"] = HOLDOUT
    print(f"\n{len(chosen)} cell(s) pass the pre-specified bar to the holdout", flush=True)
    hold = [one(r["entry"], r["direction"], r["horizon"]) for r in chosen]
    if hold:
        print("HOLDOUT 2022-02 on", flush=True)
        show(hold)
    OUT.write_text(
        json.dumps({"dev": dev, "dev_q": qs, "holdout": hold}, indent=1, default=str),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
