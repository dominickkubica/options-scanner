"""Pre-registered: does a deeper oversold reading make the bounce's spread pay more?

Written 2026-10-22, before any of the data it will judge exists. Do not change the
hypothesis, the bands, the window or the gate after the first run; a changed study is a
new study with a new name.

## Why

An exploratory run on 2026-10-22 (all of 2014-2026, so nothing here is unseen) found the
call debit spread on RSI(14) between 20 and 25 ahead of RSI between 25 and 30 in both
the development and holdout windows (edge +18% vs +17%, and +24% vs +14%), and RSI at or
below 20 behind in both, on 17 and 13 trades. For the stock the two windows disagreed.
Twenty cells were looked at, uncorrected, so that run proves nothing and only motivates
this one.

## Hypothesis

H1. The bounce's call debit spread (15 DTE, ATM long, short 0.84 sigma above, closed at
the fifth session's close, 5% slippage, on `IV_SYMBOLS`) entered when RSI(14) closes in
(20, 25] earns a higher mean return on the debit than when it closes in (25, 30].

Measured only on sessions after 2026-10-23, which no study has seen.

## Gate, fixed now

H1 passes only if all three hold:

1. the (20, 25] band beats its circular-shift null at q < 0.10 (Benjamini-Hochberg over
   the two bands);
2. its edge exceeds the (25, 30] band's edge;
3. it has at least 30 independent blocks.

Below 30 blocks the study reports and refuses to judge: say "not yet", not "no".

## When

Not before 2028-01-24. The harness drops a symbol with fewer than 260 bars inside the
window, and RSI needs 60 bars of warmup, so the first entries this window can produce
are in early 2027 and about fifteen months of sessions are needed before every symbol
counts. `main` refuses earlier runs.
"""

from __future__ import annotations

from datetime import date

from harness import IV_SYMBOLS, Study

from optscan.analytics import rules

FORWARD = (date(2026, 10, 23), date(2030, 1, 1))
EARLIEST_RUN = date(2028, 1, 24)
MIN_BLOCKS = 30
Q_MAX = 0.10

CDS = {
    "mode": "call_debit_spread",
    "dte": 15,
    "horizon": 5,
    "offset": 0.0,
    "width": 0.84,
    "slippage": 0.05,
    "in_sigmas": True,
}


def band(lo: float, hi: float):
    def fn(bars):
        r = rules.rsi_series(bars, 14)
        return [i for i in range(rules.WARMUP, len(bars)) if r[i] is not None and lo < r[i] <= hi]

    return fn


def main() -> None:
    if date.today() < EARLIEST_RUN:
        raise SystemExit(f"Pre-registered to run no earlier than {EARLIEST_RUN}.")
    study = Study("rsi_depth_study")
    study.rule("rsi_20_25", band(20, 25))
    study.rule("rsi_25_30", band(25, 30))
    with study.window(FORWARD):
        rows = [
            study.run(name, entry=name, symbols=IV_SYMBOLS, max_missing=0.9, **CDS)
            for name in ("rsi_20_25", "rsi_25_30")
        ]
    qs = study.bh(rows)
    study.show(rows, qs)
    deep, shallow = rows
    if deep.get("blocks", 0) < MIN_BLOCKS:
        verdict = f"not yet: {deep.get('blocks', 0)} blocks, the gate needs {MIN_BLOCKS}"
    elif qs[0] is not None and qs[0] < Q_MAX and deep["edge"] > shallow.get("edge", 0.0):
        verdict = "H1 passes"
    else:
        verdict = "H1 fails"
    print(verdict)
    study.save({"rows": rows, "q": qs, "verdict": verdict})


if __name__ == "__main__":
    main()
