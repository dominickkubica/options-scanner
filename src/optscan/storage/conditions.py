"""The daily condition record: one row per symbol per session.

## Why this table exists at all

Every other study in this project could be run the day it was written, because the data
was already there: daily bars go back a decade, the option tape holds two and a half
years of minute prints, and the validation store logs candidates as they are scored.

Gamma is different, and the difference is a hard fact about the data rather than a
preference. **No historical per-strike open interest exists anywhere this project can
reach.** The option tape stores trades, and volume is not open interest. yfinance
publishes open interest only as it stands today. Market Chameleon's daily export carries
call and put open interest as whole-chain totals, which can make a put/call ratio and
cannot make a wall.

So the call wall, the put wall, the pin, max pain, net gamma exposure and the zero-gamma
level cannot be backtested. Not "cannot be backtested well": the inputs for any date
before today do not exist. The only honest way to find out whether they predict anything
is to write them down every session from now on and look again once there are enough.

That is what this is. It starts the clock.

## Why the price conditions ride along

RSI, the moving averages and the tested levels could all be recomputed later from stored
bars, so storing them looks redundant. It is not, for one reason: a study that filters on
gamma *and* RSI needs both measured at the same moment on the same price. Recomputing one
side later means joining a number taken at 15:45 from a live chain to a number taken from
a daily bar that closed at 16:00, on a spot that may have come from a different vendor.
Keeping every condition on one row removes a whole class of silent mismatch, which this
project has been bitten by often enough to be worth a few extra columns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from optscan.tickers import normalize_ticker


@dataclass(frozen=True, slots=True)
class DailyCondition:
    """Every condition a later study might filter on, measured at one moment."""

    session_date: date
    symbol: str
    spot: float
    spot_source: str | None = None
    expiry: date | None = None
    dte: int | None = None

    regime: str | None = None
    total_gex: float | None = None
    zero_gamma: float | None = None
    call_wall: float | None = None
    call_wall_oi: int | None = None
    put_wall: float | None = None
    put_wall_oi: int | None = None
    pin: float | None = None
    pin_oi: int | None = None
    max_pain: float | None = None
    priced_oi: int | None = None
    total_oi: int | None = None

    rsi14: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    atr14: float | None = None
    realized_vol: float | None = None
    nearest_level: float | None = None
    nearest_level_kind: str | None = None
    iv_rank: float | None = None
    iv_rank_confidence: str | None = None

    note: str | None = None

    @property
    def above_zero_gamma(self) -> bool | None:
        """Which side of the flip spot sits on. None when there is no flip to be on."""
        if self.zero_gamma is None:
            return None
        return self.spot > self.zero_gamma

    def distance_to(self, level: float | None) -> float | None:
        """Signed distance from spot to a level, as a fraction. None stays None."""
        if level is None or not self.spot:
            return None
        return (level - self.spot) / self.spot


_COLUMNS = (
    "session_date",
    "symbol",
    "recorded_at",
    "spot",
    "spot_source",
    "expiry",
    "dte",
    "regime",
    "total_gex",
    "zero_gamma",
    "call_wall",
    "call_wall_oi",
    "put_wall",
    "put_wall_oi",
    "pin",
    "pin_oi",
    "max_pain",
    "priced_oi",
    "total_oi",
    "rsi14",
    "ema20",
    "ema50",
    "ema200",
    "atr14",
    "realized_vol",
    "nearest_level",
    "nearest_level_kind",
    "iv_rank",
    "iv_rank_confidence",
    "note",
)


def record(conn: sqlite3.Connection, conditions: DailyCondition) -> bool:
    """Store one session's conditions. Returns False when the row was already held.

    Never overwrites. A session's conditions are what they were when they were measured,
    and a second run later in the same day would otherwise quietly replace a 15:45
    reading with a 20:00 one, which is precisely the sampling-time pollution the IV
    history rule exists to prevent.
    """
    values = {
        "session_date": conditions.session_date.isoformat(),
        "symbol": normalize_ticker(conditions.symbol),
        "recorded_at": datetime.now(UTC).isoformat(),
        "spot": conditions.spot,
        "spot_source": conditions.spot_source,
        "expiry": conditions.expiry.isoformat() if conditions.expiry else None,
        "dte": conditions.dte,
        "regime": conditions.regime,
        "total_gex": conditions.total_gex,
        "zero_gamma": conditions.zero_gamma,
        "call_wall": conditions.call_wall,
        "call_wall_oi": conditions.call_wall_oi,
        "put_wall": conditions.put_wall,
        "put_wall_oi": conditions.put_wall_oi,
        "pin": conditions.pin,
        "pin_oi": conditions.pin_oi,
        "max_pain": conditions.max_pain,
        "priced_oi": conditions.priced_oi,
        "total_oi": conditions.total_oi,
        "rsi14": conditions.rsi14,
        "ema20": conditions.ema20,
        "ema50": conditions.ema50,
        "ema200": conditions.ema200,
        "atr14": conditions.atr14,
        "realized_vol": conditions.realized_vol,
        "nearest_level": conditions.nearest_level,
        "nearest_level_kind": conditions.nearest_level_kind,
        "iv_rank": conditions.iv_rank,
        "iv_rank_confidence": conditions.iv_rank_confidence,
        "note": conditions.note,
    }
    placeholders = ", ".join(f":{name}" for name in _COLUMNS)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO daily_condition ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cursor.rowcount > 0


def _row_to_condition(row: sqlite3.Row) -> DailyCondition:
    return DailyCondition(
        session_date=date.fromisoformat(row["session_date"]),
        symbol=row["symbol"],
        spot=row["spot"],
        spot_source=row["spot_source"],
        expiry=date.fromisoformat(row["expiry"]) if row["expiry"] else None,
        dte=row["dte"],
        regime=row["regime"],
        total_gex=row["total_gex"],
        zero_gamma=row["zero_gamma"],
        call_wall=row["call_wall"],
        call_wall_oi=row["call_wall_oi"],
        put_wall=row["put_wall"],
        put_wall_oi=row["put_wall_oi"],
        pin=row["pin"],
        pin_oi=row["pin_oi"],
        max_pain=row["max_pain"],
        priced_oi=row["priced_oi"],
        total_oi=row["total_oi"],
        rsi14=row["rsi14"],
        ema20=row["ema20"],
        ema50=row["ema50"],
        ema200=row["ema200"],
        atr14=row["atr14"],
        realized_vol=row["realized_vol"],
        nearest_level=row["nearest_level"],
        nearest_level_kind=row["nearest_level_kind"],
        iv_rank=row["iv_rank"],
        iv_rank_confidence=row["iv_rank_confidence"],
        note=row["note"],
    )


def history(
    conn: sqlite3.Connection,
    symbol: str | None = None,
    *,
    since: date | None = None,
) -> list[DailyCondition]:
    """Everything recorded, oldest first, for a symbol or for all of them."""
    clauses, params = [], []
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(normalize_ticker(symbol))
    if since is not None:
        clauses.append("session_date >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM daily_condition {where} ORDER BY session_date, symbol", params
    ).fetchall()
    return [_row_to_condition(row) for row in rows]


def coverage(conn: sqlite3.Connection) -> list[dict]:
    """Per symbol: how many sessions are held, and the span they cover.

    What a reader needs before asking this table a question, because the honest answer
    for the first several months is "not yet".
    """
    rows = conn.execute(
        """
        SELECT symbol,
               COUNT(*) AS sessions,
               MIN(session_date) AS first_session,
               MAX(session_date) AS last_session,
               SUM(zero_gamma IS NOT NULL) AS with_flip,
               SUM(total_oi > 0) AS with_open_interest
        FROM daily_condition
        GROUP BY symbol
        ORDER BY symbol
        """
    ).fetchall()
    return [dict(row) for row in rows]
