"""Can the validated oversold bounce be traded with a vertical spread?

The bounce is real on the stock: RSI(14) <= 30, long five sessions, and it held again on
2022-2026 at +0.96% over its null (p=0.017) in the bounce study. As a 21 day short put
held to expiry it lost, most plausibly because the edge is spent in five sessions while
the put carries sixteen more days of risk. This matches the structure to the edge.

PRE-SPECIFIED, before anything ran, on the 23 symbols with stored IV:

  pcs   put credit spread, 15 DTE (the shortest tenor IV30 may price), short strike 0.84
        sigmas out (about 20 delta), long 0.84 sigmas below it, CLOSED AFTER 5 SESSIONS
  cds   call debit spread, 15 DTE, long at the money, short 0.84 sigmas above, closed
        after 5 sessions
  stock the base rule on the same 23 symbols, five sessions, as the reference

Each against its own random-entry null. BH across the two option cells. DEV <= 2021,
HOLDOUT >= 2022-02. An option cell reaches the holdout only with dev q < 0.10 and a
positive dev edge.

CAVEAT that must travel with any number here: closing after five sessions means the exit
is PRICED, from that day's IV30, not settled. Only the expiry case is exact.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import optscan.jobs.backtest as job
from optscan.analytics import rules
from optscan.config import get_settings

OUT = Path(__file__).parent / "results" / ("cds_sensitivity.json")
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


def oversold(bars):
    series = rules.rsi_series(bars, 14)
    return [i for i in range(rules.WARMUP, len(bars)) if series[i] is not None and series[i] <= 30]


rules.REGISTRY["base_oversold"] = {"fn": oversold, "params": {}, "label": "base"}


BASE = {
    "mode": "call_debit_spread",
    "dte": 15,
    "horizon": 5,
    "offset": 0.0,
    "width": 0.84,
    "slippage": 0.05,
}
VARIANTS = [
    ("base: 15dte hold5 w.84 slip5%", {}),
    ("slippage 10%", {"slippage": 0.10}),
    ("slippage 15%", {"slippage": 0.15}),
    ("dte 21", {"dte": 21}),
    ("dte 30", {"dte": 30}),
    ("width 0.5 sigma", {"width": 0.5}),
    ("width 1.2 sigma", {"width": 1.2}),
    ("hold 3", {"horizon": 3}),
    ("hold 8", {"horizon": 8}),
    ("HELD TO EXPIRY, exact payoff", {"horizon": 30}),
    ("CONTROL every bar, base terms", {"entry": "every_bar"}),
]


def run_variant(overrides):
    cfg = {**BASE, **overrides}
    strategy = job.Strategy(
        name="cds",
        symbols=SYMBOLS,
        entry=cfg.get("entry", "base_oversold"),
        mode=cfg["mode"],
        dte=cfg["dte"],
        horizon=cfg["horizon"],
        offset=cfg["offset"],
        width=cfg["width"],
        slippage=cfg["slippage"],
        in_sigmas=True,
        draws=300,
    )
    result = job.run(settings, strategy)
    if result.stats is None or result.edge is None:
        return None
    return result


def main():
    WINDOW["range"] = (date(2014, 1, 1), date(2030, 1, 1))
    print(
        "SENSITIVITY, full 2014-2026, one knob at a time. Not a test: a fragility check.",
        flush=True,
    )
    rows = []
    for label, overrides in VARIANTS:
        r = run_variant(overrides)
        if r is None:
            print(f"  {label:32} no trades", flush=True)
            continue
        s, e = r.stats, r.edge
        rows.append(
            {
                "label": label,
                "trades": s.trades,
                "blocks": s.effective_sample,
                "mean": s.mean_return,
                "median": s.median_return,
                "win": s.win_rate,
                "null": e.null_mean,
                "edge": e.edge,
                "p": e.p_value,
                "years": [(y.year, y.trades, y.mean_return) for y in r.by_year],
            }
        )
        print(
            f"  {label:32} n {s.trades:4} blk {s.effective_sample:3} mean {s.mean_return * 100:+7.2f}% "
            f"med {s.median_return * 100:+6.2f}% null {e.null_mean * 100:+6.2f}% "
            f"edge {e.edge * 100:+7.2f}% p {e.p_value:.3f} win {s.win_rate * 100:3.0f}%",
            flush=True,
        )
    base = run_variant({})
    if base is not None:
        print()
        print("base variant by year:", flush=True)
        for y in base.by_year:
            print(f"  {y.year}  n {y.trades:3}  mean {y.mean_return * 100:+7.2f}%", flush=True)
    OUT.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
