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
from optscan.analytics.premium_stats import benjamini_hochberg
from optscan.config import get_settings

OUT = Path(__file__).parent / "results" / ("bounce_spread_results.json")
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

CELLS = {
    "pcs": {"mode": "put_credit_spread", "offset": 0.84, "width": 0.84},
    "cds": {"mode": "call_debit_spread", "offset": 0.0, "width": 0.84},
}


def one(name):
    if name == "stock":
        strategy = job.Strategy(
            name="stock",
            symbols=SYMBOLS,
            entry="base_oversold",
            mode="underlying",
            direction="long",
            horizon=5,
            cost_model="estimated",
            draws=300,
        )
    else:
        cell = CELLS[name]
        strategy = job.Strategy(
            name=name,
            symbols=SYMBOLS,
            entry="base_oversold",
            mode=cell["mode"],
            dte=15,
            horizon=5,
            offset=cell["offset"],
            width=cell["width"],
            in_sigmas=True,
            draws=300,
        )
    result = job.run(settings, strategy)
    if result.stats is None or result.edge is None:
        return {"name": name, "trades": 0}
    return {
        "name": name,
        "trades": result.stats.trades,
        "blocks": result.stats.effective_sample,
        "mean": result.stats.mean_return,
        "median": result.stats.median_return,
        "win": result.stats.win_rate,
        "null": result.edge.null_mean,
        "edge": result.edge.edge,
        "p": result.edge.p_value,
    }


def show(rows, qs=None):
    for index, row in enumerate(rows):
        if not row.get("trades"):
            print(f"  {row['name']:6} no trades", flush=True)
            continue
        q = "" if qs is None or qs[index] is None else f" q {qs[index]:.3f}"
        print(
            f"  {row['name']:6} n {row['trades']:5} blk {row['blocks']:4} "
            f"mean {row['mean'] * 100:+7.2f}% med {row['median'] * 100:+7.2f}% "
            f"null {row['null'] * 100:+7.2f}% edge {row['edge'] * 100:+7.2f}% "
            f"p {row['p']:.3f}{q} win {row['win'] * 100:3.0f}%",
            flush=True,
        )


def main():
    WINDOW["range"] = DEV
    ref = one("stock")
    tests = [one("pcs"), one("cds")]
    qs = benjamini_hochberg([row.get("p") for row in tests])
    print("DEV 2014-2021, reference", flush=True)
    show([ref])
    print("DEV option cells (BH across these two)", flush=True)
    show(tests, qs)
    chosen = [
        row["name"]
        for row, q in zip(tests, qs, strict=True)
        if q is not None and q < 0.10 and row.get("edge", -1) > 0
    ]
    WINDOW["range"] = HOLDOUT
    print(f"\n{len(chosen)} option cell(s) pass the pre-specified bar to the holdout", flush=True)
    hold_ref = one("stock")
    hold = [one(name) for name in chosen]
    print("HOLDOUT 2022-02 on, reference", flush=True)
    show([hold_ref])
    if hold:
        print("HOLDOUT, chosen option cells only", flush=True)
        show(hold)
    # Reported, not selected on: the option cells' holdout numbers regardless, so the
    # write-up can say what a spread would have done in the period where the stock edge
    # is strongest. Printed after the decision above and labelled as such.
    print("\nFOR THE RECORD ONLY (not a test): every option cell on the holdout", flush=True)
    record = [one(name) for name in CELLS]
    show(record)
    OUT.write_text(
        json.dumps(
            {
                "dev_ref": ref,
                "dev": tests,
                "dev_q": qs,
                "chosen": chosen,
                "holdout_ref": hold_ref,
                "holdout": hold,
                "record": record,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
