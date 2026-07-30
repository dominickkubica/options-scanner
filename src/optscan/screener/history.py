"""ATM implied volatility history, reconstructed from stored snapshots.

The snapshot job stores quotes, not volatilities, on purpose: vols are derived and the
derivation improves, so storing the inputs means a better solver can be applied to
history retroactively rather than only going forward.

The cost is that IV rank has to rebuild its history on demand. That is cheap enough at
one ATM vol per symbol per session, and it is why this takes a DataFrame rather than
re-solving whole chains.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from optscan.analytics.greeks import time_to_expiry
from optscan.analytics.iv import implied_vol, interpolate_atm_vol
from optscan.logging import get_logger

log = get_logger("optscan.screener.history")

#: The horizon IV rank is measured at. 30 days is the conventional constant maturity
#: point: near enough to be the vol people trade, far enough not to be dominated by
#: the gamma of the current week.
TARGET_DTE = 30


def atm_iv_history(
    frame: pd.DataFrame,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    target_dte: int = TARGET_DTE,
) -> list[tuple[date, float]]:
    """One constant maturity ATM implied vol per session, oldest first.

    For each stored session it picks the expiry nearest `target_dte`, solves the vols
    around the money, and interpolates to spot. Sessions that cannot produce one are
    skipped rather than filled: a gap in the IV history is visible in the completeness
    ratio, and an interpolated value would not be.
    """
    if frame.empty:
        return []

    required = {"session_date", "expiry", "strike", "right", "bid", "ask", "captured_at"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"snapshot frame is missing columns: {sorted(missing)}")

    history: list[tuple[date, float]] = []
    for session, session_rows in frame.groupby(frame["session_date"].dt.date, sort=True):
        value = _session_atm_iv(
            session_rows,
            rate=rate,
            dividend_yield=dividend_yield,
            target_dte=target_dte,
        )
        if value is not None:
            history.append((session, value))

    return history


def _session_atm_iv(
    rows: pd.DataFrame,
    *,
    rate: float,
    dividend_yield: float,
    target_dte: int,
) -> float | None:
    """Constant maturity ATM vol for one captured session."""
    spot = _session_spot(rows)
    if spot is None or spot <= 0:
        return None

    captured = rows["captured_at"].max()
    asof = _to_datetime(captured)
    if asof is None:
        return None

    expiries = sorted({value.date() for value in rows["expiry"]})
    if not expiries:
        return None

    session_date = asof.date()
    future = [expiry for expiry in expiries if (expiry - session_date).days >= 0]
    if not future:
        return None
    chosen = min(future, key=lambda expiry: abs((expiry - session_date).days - target_dte))

    time = time_to_expiry(chosen, asof)
    if time <= 0:
        return None

    near = rows[
        (rows["expiry"].dt.date == chosen)
        & (rows["strike"] >= spot * 0.9)
        & (rows["strike"] <= spot * 1.1)
    ]
    if near.empty:
        return None

    vols: dict[float, float] = {}
    for row in near.itertuples():
        bid, ask = row.bid, row.ask
        if pd.isna(bid) or pd.isna(ask) or bid <= 0 or ask <= 0 or bid > ask:
            continue
        mid = (bid + ask) / 2.0
        result = implied_vol(row.right, mid, spot, float(row.strike), time, rate, dividend_yield)
        if result.ok and result.sigma:
            # Keep whichever of the pair solved; both sides of the same strike should
            # agree, and averaging a solved one with a missing one is not an average.
            vols[float(row.strike)] = result.sigma

    return interpolate_atm_vol(vols, spot)


def _session_spot(rows: pd.DataFrame) -> float | None:
    """Underlying price for a session, preferring the stored quote over chain data."""
    for column in ("quote_last", "underlying_price"):
        if column not in rows.columns:
            continue
        values = rows[column].dropna()
        if not values.empty:
            value = float(values.iloc[0])
            if value > 0:
                return value
    return None


def _to_datetime(value) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize(UTC) if stamp.tzinfo is None else stamp.tz_convert(UTC)
    return stamp.to_pydatetime()
