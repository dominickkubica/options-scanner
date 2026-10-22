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

from harness import DEV, HOLDOUT, IV_SYMBOLS, Study, oversold

study = Study("bounce_spread_results")
study.rule("base_oversold", oversold)

CELLS = {
    "pcs": {"mode": "put_credit_spread", "offset": 0.84, "width": 0.84},
    "cds": {"mode": "call_debit_spread", "offset": 0.0, "width": 0.84},
}


def stock():
    return study.run(
        "stock",
        symbols=IV_SYMBOLS,
        entry="base_oversold",
        mode="underlying",
        direction="long",
        horizon=5,
        cost_model="estimated",
    )


def option(name):
    cell = CELLS[name]
    return study.run(
        name,
        symbols=IV_SYMBOLS,
        entry="base_oversold",
        mode=cell["mode"],
        dte=15,
        horizon=5,
        offset=cell["offset"],
        width=cell["width"],
        in_sigmas=True,
    )


def main():
    with study.window(DEV):
        ref = stock()
        tests = [option("pcs"), option("cds")]
    qs = study.bh(tests)
    print("DEV 2014-2021, reference", flush=True)
    study.show([ref])
    print("DEV option cells (BH across these two)", flush=True)
    study.show(tests, qs)
    chosen = [row["label"] for row in study.gate(tests, qs)]
    print()
    print(f"{len(chosen)} option cell(s) pass the pre-specified bar to the holdout", flush=True)
    with study.window(HOLDOUT):
        hold_ref = stock()
        hold = [option(name) for name in chosen]
        print("HOLDOUT 2022-02 on, reference", flush=True)
        study.show([hold_ref])
        if hold:
            print("HOLDOUT, chosen option cells only", flush=True)
            study.show(hold)
        # Reported, not selected on, and labelled as such.
        print()
        print("FOR THE RECORD ONLY (not a test): every option cell on the holdout", flush=True)
        record = [option(name) for name in CELLS]
    study.show(record)
    study.save(
        {
            "dev_ref": ref,
            "dev": tests,
            "dev_q": qs,
            "chosen": chosen,
            "holdout_ref": hold_ref,
            "holdout": hold,
            "record": record,
        }
    )


if __name__ == "__main__":
    main()
