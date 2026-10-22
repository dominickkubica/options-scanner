"""Can the one validated edge be sharpened? oversold_bounce, filtered.

`oversold_bounce` (RSI(14) <= 30, long the stock, five sessions) is the only strategy in
this project that survived a pre-specified out of sample test. This asks whether three
filters, each with prior support and each named here before anything ran, improve it:

  oversold_bull   the market is in an uptrend: SPY's close above its own 200 day mean
  oversold_idio   the stock is oversold but the market is not: SPY's RSI(14) above 40
  oversold_fear   the stock's IV30 / 20 day realized vol at or above its dev median
                  (only the 23 symbols with stored IV; compared against the base rule
                  on those same 23)

Underlying mode, long, five sessions, every symbol's own estimated round trip cost.
DEV <= 2021-12-31, HOLDOUT >= 2022-02-01. BH across the three dev tests. A filter goes
to the holdout only if its dev q < 0.10 and its dev edge beats the base rule's dev edge,
because a filter that does not improve on what it filters is not worth having.

Market and vol conditions are keyed by date and computed from closes up to and including
that date. The stock's own RSI is computed by the rule on the bars it is given, exactly
as the validated `rsi_below` does, so the base and the filters see identical RSI.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np

import optscan.jobs.backtest as job
from optscan.analytics import rules
from optscan.analytics.premium_stats import benjamini_hochberg
from optscan.config import get_settings

OUT = Path(__file__).parent / "results" / ("bounce_study_results.json")
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

# Market conditions from SPY's full history, keyed by date.
_spy = _original_load(settings, ["SPY"])[0]["SPY"]
_spy_rsi = rules.rsi_series(_spy, 14)
MARKET: dict[date, dict] = {}
for i in range(200, len(_spy)):
    closes = [bar.close for bar in _spy[i - 199 : i + 1]]
    MARKET[_spy[i].ts.date()] = {
        "bull": _spy[i].close > sum(closes) / len(closes),
        "rsi": _spy_rsi[i],
    }

# Vol richness for the IV symbols, keyed by date.
VOL: dict[str, dict[date, float]] = {}
_iv_series, _ = _original_load(settings, IV_SYMBOLS)
for symbol, bars in _iv_series.items():
    iv = job.load_implied(settings, symbol)
    closes = [bar.close for bar in bars]
    per = {}
    for i in range(21, len(bars)):
        current = iv.get(bars[i].ts.date())
        if not current:
            continue
        rets = [
            math.log(closes[k] / closes[k - 1]) for k in range(i - 19, i + 1) if closes[k - 1] > 0
        ]
        rv = float(np.std(rets, ddof=1)) * math.sqrt(252) if len(rets) > 10 else 0
        if rv > 0:
            per[bars[i].ts.date()] = current / rv
    VOL[symbol] = per

_dev_ratios = [v for per in VOL.values() for d, v in per.items() if DEV[0] <= d <= DEV[1]]
RATIO_MEDIAN = float(np.median(_dev_ratios))


def oversold(bars):
    series = rules.rsi_series(bars, 14)
    return [i for i in range(rules.WARMUP, len(bars)) if series[i] is not None and series[i] <= 30]


def filtered(test):
    def fn(bars):
        if not bars:
            return []
        symbol = bars[0].symbol
        return [i for i in oversold(bars) if test(symbol, bars[i].ts.date())]

    return fn


def _bull(symbol, day):
    row = MARKET.get(day)
    return bool(row and row["bull"])


def _idio(symbol, day):
    row = MARKET.get(day)
    return bool(row and row["rsi"] is not None and row["rsi"] > 40)


def _fear(symbol, day):
    value = VOL.get(symbol, {}).get(day)
    return value is not None and value >= RATIO_MEDIAN


rules.REGISTRY["base_oversold"] = {"fn": oversold, "params": {}, "label": "base"}
rules.REGISTRY["oversold_bull"] = {"fn": filtered(_bull), "params": {}, "label": "bull"}
rules.REGISTRY["oversold_idio"] = {"fn": filtered(_idio), "params": {}, "label": "idio"}
rules.REGISTRY["oversold_fear"] = {"fn": filtered(_fear), "params": {}, "label": "fear"}


def one(entry, symbols):
    strategy = job.Strategy(
        name=entry,
        symbols=symbols,
        entry=entry,
        mode="underlying",
        direction="long",
        horizon=5,
        cost_model="estimated",
        draws=300,
    )
    result = job.run(settings, strategy)
    if result.stats is None or result.edge is None:
        return {"entry": entry, "trades": 0}
    return {
        "entry": entry,
        "universe": "iv23" if symbols else "all",
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
            print(f"  {row['entry']:15} no trades", flush=True)
            continue
        q = "" if qs is None or qs[index] is None else f" q {qs[index]:.3f}"
        print(
            f"  {row['entry']:15} {row['universe']:5} n {row['trades']:6} blk {row['blocks']:4} "
            f"mean {row['mean'] * 100:+6.3f}% null {row['null'] * 100:+6.3f}% "
            f"edge {row['edge'] * 100:+6.3f}% p {row['p']:.3f}{q} win {row['win'] * 100:3.0f}%",
            flush=True,
        )


def main():
    print(f"vol ratio dev median {RATIO_MEDIAN:.3f}", flush=True)
    WINDOW["range"] = DEV
    base_all = one("base_oversold", [])
    base_iv = one("base_oversold", IV_SYMBOLS)
    tests = [
        one("oversold_bull", []),
        one("oversold_idio", []),
        one("oversold_fear", IV_SYMBOLS),
    ]
    qs = benjamini_hochberg([row.get("p") for row in tests])
    print("\nDEV 2014-2021, base rule", flush=True)
    show([base_all, base_iv])
    print("DEV filters (BH across these three)", flush=True)
    show(tests, qs)

    baseline = {"all": base_all.get("edge", 0), "iv23": base_iv.get("edge", 0)}
    chosen = [
        row
        for row, q in zip(tests, qs, strict=True)
        if q is not None and q < 0.10 and row.get("edge", -1) > baseline[row["universe"]]
    ]
    WINDOW["range"] = HOLDOUT
    print(f"\n{len(chosen)} filter(s) pass the pre-specified bar to the holdout", flush=True)
    hold_base = [one("base_oversold", []), one("base_oversold", IV_SYMBOLS)]
    hold_tests = [
        one(row["entry"], IV_SYMBOLS if row["universe"] == "iv23" else []) for row in chosen
    ]
    print("HOLDOUT 2022-02 on, base rule", flush=True)
    show(hold_base)
    if hold_tests:
        print("HOLDOUT, chosen filters only", flush=True)
        show(hold_tests)
    OUT.write_text(
        json.dumps(
            {
                "ratio_median": RATIO_MEDIAN,
                "dev_base": [base_all, base_iv],
                "dev_tests": tests,
                "dev_q": qs,
                "holdout_base": hold_base,
                "holdout_tests": hold_tests,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
