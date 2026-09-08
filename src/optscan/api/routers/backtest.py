"""Backtesting over HTTP.

Unlike every other route in this API, this one does real work: a run over the whole
universe is seconds, and a level rule over three hundred symbols is minutes. So the
inputs are bounded here rather than trusted, and the bounds are stated in the response
instead of silently applied.

The response deliberately omits the trade rows. A decade across three hundred symbols is
tens of thousands of them, the UI charts the curve rather than the ledger, and shipping
the ledger would make the payload larger than everything else this API returns put
together. `optscan backtest --save` writes them for anyone who wants them.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, status

from optscan.analytics import rules as rule_registry
from optscan.api.deps import SettingsDep
from optscan.api.schemas import BacktestOut, BacktestRuleOut
from optscan.jobs.backtest import MAX_LEVEL_SYMBOLS, Strategy, run
from optscan.universe import UniverseError

router = APIRouter(tags=["backtest"])

#: Symbols one request may test. Above this the run stops being something a browser
#: waits for; the CLI has no such bound.
MAX_SYMBOLS = 120

#: Null draws one request may ask for. A thousand is the engine's default and resolves a
#: p-value to 0.001, which is finer than anything here is entitled to claim.
MAX_DRAWS = 2000


@router.get("/backtest/rules", response_model=list[BacktestRuleOut])
def rules() -> list[BacktestRuleOut]:
    """The entry rules and their parameters, so the UI offers exactly what exists."""
    return [BacktestRuleOut(**row) for row in rule_registry.describe()]


@router.post("/backtest", response_model=BacktestOut)
def run_backtest(settings: SettingsDep, spec: dict) -> BacktestOut:
    """Run one strategy. Bounded, and the bounds are reported rather than hidden."""
    started = time.perf_counter()
    notes: list[str] = []

    try:
        strategy = Strategy.from_dict(spec)
        strategy.validate()
    except (ValueError, TypeError) as error:
        # A bad spec is the caller's mistake and the message names what was wrong, so a
        # 400 with the text is more useful than a generic 422 from the model layer.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    if len(strategy.symbols) > MAX_SYMBOLS:
        notes.append(
            f"Testing the first {MAX_SYMBOLS} of {len(strategy.symbols)} symbols. "
            "The CLI has no such limit; this route has to answer while a browser waits."
        )
        strategy.symbols = strategy.symbols[:MAX_SYMBOLS]

    if strategy.draws > MAX_DRAWS:
        notes.append(f"Null draws capped at {MAX_DRAWS}.")
        strategy.draws = MAX_DRAWS

    if not strategy.symbols and not strategy.group:
        notes.append(
            f"No symbols or group given, so the run covers everything with stored bars, "
            f"capped at {MAX_LEVEL_SYMBOLS} symbols for the level rules."
        )

    try:
        result = run(settings, strategy)
    except UniverseError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    payload = result.as_dict()
    payload.pop("trades", None)
    payload["notes"] = notes + list(payload.get("notes", []))
    payload["strategy"] = strategy.to_dict()
    payload["seconds"] = round(time.perf_counter() - started, 2)
    return BacktestOut(**payload)
