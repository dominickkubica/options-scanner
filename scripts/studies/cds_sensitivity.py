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

from harness import FULL, IV_SYMBOLS, Study, oversold

study = Study("cds_sensitivity")
study.rule("base_oversold", oversold)

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


def run_variant(label, overrides):
    cfg = {**BASE, **overrides}
    entry = cfg.pop("entry", "base_oversold")
    return study.run(label, symbols=IV_SYMBOLS, entry=entry, in_sigmas=True, **cfg)


def main():
    print(
        "SENSITIVITY, full 2014-2026, one knob at a time. Not a test: a fragility check.",
        flush=True,
    )
    with study.window(FULL):
        rows = [run_variant(label, overrides) for label, overrides in VARIANTS]
    study.show(rows)
    print()
    print("base variant by year:", flush=True)
    for year, trades, mean in rows[0].get("years", []):
        print(f"  {year}  n {trades:3}  mean {mean * 100:+7.2f}%", flush=True)
    study.save({"variants": rows})


if __name__ == "__main__":
    main()
