"""1b: the same vol-timing test with the strike held at a constant ~20 delta.

Study 1 sold a fixed 5% out of the money, which is a much higher delta when IV is high,
so "sell when IV is rich" was also "sell nearer the money when markets are wild". That
confound was identified from the mechanism after study 1 returned a null; this re-runs
the identical conditions, thresholds and selection rule with only the strike changed to
0.84 sigmas (and the width to 0.84 sigmas), put credit spreads only.

Original docstring follows.

Does timing premium sales on the volatility itself produce an entry edge?

Every entry tested so far conditioned on price. The reason premium selling pays at all
is the variance risk premium -- implied vol overpricing what is then realized -- and the
literature says that premium varies over time. So the conditions here are on vol.

PRE-SPECIFIED, before anything ran:

  vrp_high     IV30 / 20 day realized vol in its top tercile (thresholds from dev only)
  ivrank_high  IV30's rank within its own trailing 252 sessions, top tercile
  iv_spike     IV30 / its own prior 20 session average, top tercile
  oversold_rich  RSI(14) <= 30 (the validated rule) AND the vrp ratio >= dev median

each on short_put and put_credit_spread, 21 DTE, HELD TO EXPIRY so the payoff is exact
stored history and only the entry credit is modelled.

DEV is 2014-01-01..2021-12-31. HOLDOUT is 2022-02-01..end (a month of margin so no dev
trade settles inside it). BH across the eight dev tests. **The selection rule is fixed
here**: a cell goes to the holdout only if its dev q < 0.10 and its dev edge is
positive. Nothing else is looked at on the holdout. Controls (every bar) run in both.

No lookahead: conditions are computed per date from data up to and including that
date's close, on the full history, and the engine fills the next session's open.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np

import optscan.jobs.backtest as job
from optscan.analytics import rules
from optscan.analytics.premium_stats import benjamini_hochberg
from optscan.config import get_settings

OUT = Path(__file__).parent / "results" / ("vrp_delta_results.json")
SYMBOLS = [
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
DEV = (date(2014, 1, 1), date(2021, 12, 31))
HOLDOUT = (date(2022, 2, 1), date(2030, 1, 1))
CONDITIONS = ("vrp_high", "ivrank_high", "iv_spike", "oversold_rich")
MODES = ("put_credit_spread",)
DTE = 21

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


def build_conditions():
    """Per symbol, per date: the vol and price conditions, from data up to that date."""
    full, _ = _original_load(settings, SYMBOLS)
    table: dict[str, dict[date, dict]] = {}
    for symbol, bars in full.items():
        iv = job.load_implied(settings, symbol)
        closes = [bar.close for bar in bars]
        rsi = rules.rsi_series(bars, 14)
        logret = [0.0] + [
            math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0
            for i in range(1, len(closes))
        ]
        ivs = [iv.get(bar.ts.date()) for bar in bars]
        per: dict[date, dict] = {}
        for i in range(260, len(bars)):
            current = ivs[i]
            if not current:
                continue
            window = logret[i - 19 : i + 1]
            rv = float(np.std(window, ddof=1)) * math.sqrt(252)
            past = [v for v in ivs[i - 251 : i + 1] if v]
            prior20 = [v for v in ivs[i - 20 : i] if v]
            if rv <= 0 or len(past) < 200 or len(prior20) < 15:
                continue
            low, high = min(past), max(past)
            per[bars[i].ts.date()] = {
                "ratio": current / rv,
                "rank": (current - low) / (high - low) if high > low else 0.5,
                "spike": current / (sum(prior20) / len(prior20)),
                "rsi": rsi[i],
            }
        table[symbol] = per
    return table


CONDS = build_conditions()


def dev_values(key):
    lo, hi = DEV
    return [
        row[key]
        for per in CONDS.values()
        for day, row in per.items()
        if lo <= day <= hi and row.get(key) is not None
    ]


EDGES = {
    "ratio_t3": float(np.quantile(dev_values("ratio"), 2 / 3)),
    "ratio_med": float(np.quantile(dev_values("ratio"), 1 / 2)),
    "rank_t3": float(np.quantile(dev_values("rank"), 2 / 3)),
    "spike_t3": float(np.quantile(dev_values("spike"), 2 / 3)),
}


def make_rule(test):
    def fn(bars):
        per = CONDS.get(bars[0].symbol, {}) if bars else {}
        out = []
        for index, bar in enumerate(bars):
            row = per.get(bar.ts.date())
            if row is None:
                continue
            if test(row):
                out.append(index)
        return out

    return fn


TESTS = {
    "vrp_high": lambda r: r["ratio"] >= EDGES["ratio_t3"],
    "ivrank_high": lambda r: r["rank"] >= EDGES["rank_t3"],
    "iv_spike": lambda r: r["spike"] >= EDGES["spike_t3"],
    "oversold_rich": lambda r: (
        r["rsi"] is not None and r["rsi"] <= 30 and r["ratio"] >= EDGES["ratio_med"]
    ),
}
for name, test in TESTS.items():
    rules.REGISTRY[name] = {"fn": make_rule(test), "params": {}, "label": name}


def one(entry, mode):
    strategy = job.Strategy(
        name=f"{entry}/{mode}",
        symbols=SYMBOLS,
        entry=entry,
        mode=mode,
        dte=DTE,
        horizon=21,
        draws=300,
        offset=0.84,
        width=0.84,
        in_sigmas=True,
    )
    result = job.run(settings, strategy)
    if result.stats is None or result.edge is None:
        return {"entry": entry, "mode": mode, "trades": 0}
    return {
        "entry": entry,
        "mode": mode,
        "trades": result.stats.trades,
        "blocks": result.stats.effective_sample,
        "mean": result.stats.mean_return,
        "win": result.stats.win_rate,
        "null": result.edge.null_mean,
        "edge": result.edge.edge,
        "p": result.edge.p_value,
    }


def show(rows, qs=None):
    for index, row in enumerate(rows):
        if not row.get("trades"):
            print(f"  {row['entry']:14} {row['mode']:18} no trades", flush=True)
            continue
        q = "" if qs is None or qs[index] is None else f" q {qs[index]:.3f}"
        print(
            f"  {row['entry']:14} {row['mode']:18} n {row['trades']:5} blk {row['blocks']:4} "
            f"mean {row['mean'] * 100:+6.2f}% null {row['null'] * 100:+6.2f}% "
            f"edge {row['edge'] * 100:+6.2f}% p {row['p']:.3f}{q} win {row['win'] * 100:3.0f}%",
            flush=True,
        )


def main():
    print("thresholds from dev:", {k: round(v, 3) for k, v in EDGES.items()}, flush=True)
    WINDOW["range"] = DEV
    controls = [one("every_bar", mode) for mode in MODES]
    tests = [one(cond, mode) for cond in CONDITIONS for mode in MODES]
    qs = benjamini_hochberg([row.get("p") for row in tests])
    print("\nDEV 2014-2021 controls", flush=True)
    show(controls)
    print("DEV tests (BH across these eight)", flush=True)
    show(tests, qs)

    chosen = [
        row
        for row, q in zip(tests, qs, strict=True)
        if q is not None and q < 0.10 and row.get("edge", 0) > 0
    ]
    WINDOW["range"] = HOLDOUT
    print(f"\n{len(chosen)} cell(s) pass the pre-specified bar to the holdout", flush=True)
    hold_controls = [one("every_bar", mode) for mode in MODES]
    hold_tests = [one(row["entry"], row["mode"]) for row in chosen]
    print("HOLDOUT 2022-02 on, controls", flush=True)
    show(hold_controls)
    if hold_tests:
        print("HOLDOUT, the chosen cells only", flush=True)
        show(hold_tests)

    OUT.write_text(
        json.dumps(
            {
                "edges": EDGES,
                "dev_controls": controls,
                "dev_tests": tests,
                "dev_q": qs,
                "holdout_controls": hold_controls,
                "holdout_tests": hold_tests,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
