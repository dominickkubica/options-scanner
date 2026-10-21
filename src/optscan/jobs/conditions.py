"""Record every condition a future study might filter on, once per session.

Reuses what already exists rather than recomputing anything its own way: the gamma
profile comes from `analytics/gex.py` through the same chain loader the dashboard panel
uses, the indicators from `analytics/levels.py`, and the IV rank from the same
`load_iv_history` the scan and the record job call. If this file ever disagrees with a
panel, that is a bug in this file.

The job is deliberately tolerant. A symbol whose chain fails still gets a row with its
price conditions and null gamma, because a row with half the columns is a usable
observation and a missing row is a hole in a series that cannot be filled in later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from optscan.analytics.gex import OiRow, build_profile
from optscan.analytics.greeks import time_to_expiry
from optscan.analytics.levels import atr, build_levels, ema, realized_volatility, rsi
from optscan.api.deps import oi_chain, oi_expiries, vendor_symbol
from optscan.config import Settings
from optscan.jobs.load import latest_snapshot
from optscan.jobs.scan import load_iv_history
from optscan.logging import get_logger
from optscan.market_calendar import market_local_date
from optscan.models import PriceBar
from optscan.screener.context import analyze_snapshot
from optscan.storage import db
from optscan.storage.conditions import DailyCondition, record
from optscan.storage.vendor import recent_daily_bars

log = get_logger("optscan.jobs.conditions")

#: Sessions of history behind the indicators. Enough for a 200 period EMA to mean
#: something, which is the longest window recorded.
HISTORY_SESSIONS = 400

#: Minimum bars before an indicator is computed at all. A 200 EMA from 30 sessions is a
#: number, not a measurement.
MIN_BARS = 220


@dataclass
class ConditionResult:
    recorded: int = 0
    already_held: int = 0
    symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_record_conditions(
    settings: Settings,
    *,
    symbols: list[str] | None = None,
    asof: date | None = None,
    provider_factory=None,
) -> ConditionResult:
    """Write one row per symbol for today's session, skipping what is already held."""
    session = asof or market_local_date(datetime.now(UTC), settings.market_timezone)
    result = ConditionResult()

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        targets = [s.strip().upper() for s in symbols] if symbols else db.list_watchlist(conn)

        for symbol in targets:
            conditions, note = _conditions_for(settings, conn, symbol, session, provider_factory)
            if conditions is None:
                result.notes.append(note or f"{symbol} produced nothing to record.")
                continue
            if record(conn, conditions):
                result.recorded += 1
            else:
                result.already_held += 1
            result.symbols.append(symbol)
            if note:
                result.notes.append(note)

    log.info(
        "recorded conditions",
        session=session.isoformat(),
        recorded=result.recorded,
        held=result.already_held,
    )
    return result


def _conditions_for(
    settings: Settings,
    conn,
    symbol: str,
    session: date,
    provider_factory=None,
) -> tuple[DailyCondition | None, str | None]:
    """One symbol's row, or a stated reason there is none."""
    bars, _ = recent_daily_bars(conn, symbol, HISTORY_SESSIONS)
    price_bars = _as_bars(symbol, bars)

    gamma, spot, spot_source, expiry, gamma_note = _gamma(settings, symbol, provider_factory)

    # Three sources, in descending order of how well they match the gamma above: the
    # chain's own underlying, then the last stored capture's quote, then the most recent
    # daily close. Which one was used is written down, because they are different prices
    # taken at different moments and a later study comparing two symbols should be able
    # to see that it is comparing two kinds of number.
    if spot is None:
        snapshot = latest_snapshot(settings.snapshot_path, symbol)
        if snapshot is not None and snapshot.quote.price:
            spot, spot_source = snapshot.quote.price, "stored capture quote"
    if spot is None and price_bars:
        spot, spot_source = price_bars[-1].close, "daily close"
    if spot is None:
        return None, f"{symbol} has no chain, no capture and no stored bar; nothing recorded."

    iv_rank, iv_confidence = _iv_rank(settings, symbol)

    nearest, nearest_kind = _nearest_level(price_bars, spot)
    enough = len(price_bars) >= MIN_BARS

    return (
        DailyCondition(
            session_date=session,
            symbol=symbol,
            spot=spot,
            spot_source=spot_source,
            expiry=expiry,
            dte=(expiry - session).days if expiry else None,
            regime=gamma.regime if gamma else None,
            total_gex=gamma.total_gex if gamma else None,
            zero_gamma=gamma.zero_gamma if gamma else None,
            call_wall=gamma.call_wall.strike if gamma and gamma.call_wall else None,
            call_wall_oi=gamma.call_wall.open_interest if gamma and gamma.call_wall else None,
            put_wall=gamma.put_wall.strike if gamma and gamma.put_wall else None,
            put_wall_oi=gamma.put_wall.open_interest if gamma and gamma.put_wall else None,
            pin=gamma.pin.strike if gamma and gamma.pin else None,
            pin_oi=gamma.pin.open_interest if gamma and gamma.pin else None,
            max_pain=gamma.max_pain if gamma else None,
            priced_oi=gamma.priced_oi if gamma else None,
            total_oi=gamma.total_oi if gamma else None,
            rsi14=rsi(price_bars) if price_bars else None,
            ema20=ema(price_bars, 20) if price_bars else None,
            ema50=ema(price_bars, 50) if price_bars else None,
            ema200=ema(price_bars, 200) if enough else None,
            atr14=atr(price_bars) if price_bars else None,
            realized_vol=realized_volatility(price_bars) if price_bars else None,
            nearest_level=nearest,
            nearest_level_kind=nearest_kind,
            iv_rank=iv_rank,
            iv_rank_confidence=iv_confidence,
            note=gamma_note,
        ),
        None,
    )


def _as_bars(symbol: str, stored) -> list[PriceBar]:
    """Stored rows as PriceBars, dropping any the model refuses rather than guessing."""
    bars: list[PriceBar] = []
    for row in stored or ():
        try:
            bars.append(
                PriceBar(
                    symbol=symbol,
                    ts=datetime.combine(row.session_date, datetime.min.time(), tzinfo=UTC),
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    fetched_at=datetime.now(UTC),
                    source=row.source,
                )
            )
        except (ValueError, AttributeError):
            continue
    return bars


def _gamma(settings: Settings, symbol: str, provider_factory=None):
    """The gamma profile for the nearest expiry still trading, or Nones and a reason.

    The provider is injected rather than reached for, so a test that has overridden it
    is not bypassed by this path going to the network on its own. That exact mistake is
    documented on `_QUOTE_PROVIDER` in api/deps.py.
    """
    listed, note = oi_expiries(symbol, provider_factory=provider_factory)
    if not listed:
        return None, None, None, None, note

    now = datetime.now(UTC)
    live = [item for item in listed if time_to_expiry(item, now) > 0.0]
    expiry = live[0] if live else listed[0]

    chain, chain_note = oi_chain(symbol, expiry, provider_factory=provider_factory)
    if chain is None:
        return None, None, None, expiry, chain_note

    spot = chain.underlying_price
    snapshot = latest_snapshot(settings.snapshot_path, symbol)
    if spot is None and snapshot is not None:
        spot = snapshot.quote.price
    if spot is None or spot <= 0:
        return None, None, None, expiry, f"{vendor_symbol(symbol)} chain carried no spot."

    years = time_to_expiry(expiry, now)
    rows = [
        OiRow(
            strike=contract.strike,
            right=contract.right,
            open_interest=contract.open_interest or 0,
            iv=contract.vendor_iv,
            years=years,
        )
        for contract in chain.contracts
    ]
    profile = build_profile(rows, spot, settings.risk_free_rate)
    return profile, spot, "chain underlying", expiry, "; ".join(profile.notes) or None


def _iv_rank(settings: Settings, symbol: str) -> tuple[float | None, str | None]:
    """The same rank the dashboard shows, obtained the same way.

    Through `analyze_snapshot` with the history attached, rather than ranking the series
    here. Solving it independently is how two surfaces end up disagreeing about one
    number, and the record job spent six weeks logging nulls for exactly that reason.
    """
    snapshot = latest_snapshot(settings.snapshot_path, symbol)
    if snapshot is None:
        return None, None
    try:
        analysis = analyze_snapshot(
            snapshot,
            rate=settings.risk_free_rate,
            iv_history=load_iv_history(settings, symbol, snapshot) or None,
        )
    except (ValueError, KeyError):
        return None, None
    rank = analysis.iv_rank
    if rank is None or rank.rank is None:
        return None, None
    return rank.rank, str(rank.confidence)


def _nearest_level(bars: list[PriceBar], spot: float) -> tuple[float | None, str | None]:
    """The tested level closest to spot, with the kind that earned it.

    `build_levels` only publishes levels that beat chance, so a None here means price is
    not near anything that has been shown to matter, which is itself a condition worth
    recording.
    """
    if len(bars) < MIN_BARS:
        return None, None
    try:
        levels = build_levels(bars, spot=spot)
    except (ValueError, ZeroDivisionError):
        return None, None
    candidates = levels.all()
    if not candidates:
        return None, None
    nearest = min(candidates, key=lambda level: abs(level.price - spot))
    return nearest.price, str(nearest.kind)
