"""ATM implied volatility history, reconstructed from stored snapshots.

The snapshot job stores quotes, not volatilities, on purpose: vols are derived and the
derivation improves, so storing the inputs means a better solver can be applied to
history retroactively rather than only going forward.

The cost is that IV rank has to rebuild its history on demand. That is cheap enough at
one ATM vol per symbol per session, and it is why this takes a DataFrame rather than
re-solving whole chains.

**One series, one vendor.** Phase 5 made a provider switch possible, and an IV history
that pools yfinance marks with Tradier marks is corrupt in a way nothing downstream can
detect. Two vendors disagree about the mid of a wide contract, about which strikes are
quoted at all, and about what time of day their snapshot represents; the difference
between them is a level shift, and a level shift inside the window IV rank normalizes
against moves the rank without moving the market. So the source is a required argument
here rather than a filter somebody might remember to apply, and whatever gets excluded
is counted and reported rather than dropped quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class IvHistory:
    """One vendor's ATM vol series, with whatever was left out of it named."""

    points: list[tuple[date, float]] = field(default_factory=list)
    source: str | None = None
    excluded: dict[str, int] = field(default_factory=dict)
    #: This vendor's own most recent reading, excluded from `points` above.
    #:
    #: A rank places a value inside a range, so the value and the range have to be the
    #: same quantity measured the same way. Ranking a locally solved ATM vol inside a
    #: downloaded vendor's range is the pooling rule broken in a new place: measured on
    #: 2026-09-02 the two disagree by -3.2% on QQQ and +2.9% on AAPL, in opposite
    #: directions, so there is not even a bias that could be corrected for.
    #:
    #: None means the caller should read today's vol off the chain, which is right for
    #: a series this project solved for itself out of its own captures.
    current: float | None = None
    #: Whether this came from an imported file rather than this project's own
    #: captures. Stated in the UI: every other number on the dashboard says where
    #: it came from, and a rank sourced from a vendor download while the chain
    #: beside it comes from somewhere else is exactly the case that needs saying.
    downloaded: bool = False

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self):
        return iter(self.points)

    def __bool__(self) -> bool:
        return bool(self.points)

    @property
    def excluded_sessions(self) -> int:
        return sum(self.excluded.values())

    def note(self) -> str | None:
        """A sentence for the UI, or None when nothing was excluded.

        Worth showing even though it makes the rank look worse than it could. A user
        who switched providers last week and sees the confidence drop back to
        insufficient needs to know it is the switch and not a broken job.
        """
        if self.downloaded:
            return (
                f"Ranked against {len(self.points)} sessions of imported {self.source} "
                "history, including today's reading from the same source. The chain "
                "shown elsewhere on this page is a different vendor's, so it is not "
                "mixed in: two vendors' implied vols are not one series."
            )
        if not self.excluded:
            return None
        others = ", ".join(f"{count} from {name}" for name, count in sorted(self.excluded.items()))
        return (
            f"IV history uses only the {len(self.points)} sessions captured from "
            f"{self.source}. Excluded: {others}. Implied vols from two vendors are not "
            "one series, and pooling them would move the rank without the market moving."
        )


def atm_iv_history(
    frame: pd.DataFrame,
    *,
    rate: float,
    source: str,
    dividend_yield: float = 0.0,
    target_dte: int = TARGET_DTE,
) -> IvHistory:
    """One constant maturity ATM implied vol per session, oldest first, for one vendor.

    For each stored session it picks the expiry nearest `target_dte`, solves the vols
    around the money, and interpolates to spot. Sessions that cannot produce one are
    skipped rather than filled: a gap in the IV history is visible in the completeness
    ratio, and an interpolated value would not be.

    `source` is required and is matched against the stored `source` column. Sessions
    from any other vendor are excluded and counted, never merged.
    """
    if frame.empty:
        return IvHistory(source=source)

    required = {"session_date", "expiry", "strike", "right", "bid", "ask", "captured_at", "source"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"snapshot frame is missing columns: {sorted(missing)}")

    matching = frame[frame["source"] == source]
    excluded = _excluded_sessions(frame, source)
    if matching.empty:
        if excluded:
            log.warning(
                "no IV history for the configured source",
                source=source,
                excluded=excluded,
            )
        return IvHistory(source=source, excluded=excluded)

    history: list[tuple[date, float]] = []
    for session, session_rows in matching.groupby(matching["session_date"].dt.date, sort=True):
        value = _session_atm_iv(
            session_rows,
            rate=rate,
            dividend_yield=dividend_yield,
            target_dte=target_dte,
        )
        if value is not None:
            history.append((session, value))

    return IvHistory(points=history, source=source, excluded=excluded)


def _excluded_sessions(frame: pd.DataFrame, source: str) -> dict[str, int]:
    """Distinct sessions per other vendor. Sessions, not rows: a chain is thousands of
    rows and a count of those would read as a much bigger loss than it is."""
    other = frame[frame["source"] != source]
    if other.empty:
        return {}
    counts = other.groupby("source")["session_date"].nunique()
    return {str(name): int(count) for name, count in counts.items()}


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


def vendor_iv_history(points: list[tuple[date, float]], source: str) -> IvHistory:
    """Wrap a downloaded vendor series, splitting its latest reading off as `current`.

    Nothing is excluded here because the query that produced the points already
    filtered on source: a downloaded file is one vendor by construction, unlike the
    snapshot table, which can hold captures from several.

    The split does two jobs at once and both are required:

      - It gives the rank a current value from the **same vendor** as the range it
        will be measured against. Ranking a locally solved vol inside a downloaded
        vendor's range is the pooling rule broken in a new place.
      - It keeps today out of its own history. `iv_rank` says so explicitly: a value
        that is part of the range it is being placed in is biased toward the middle,
        and most so in exactly the calm stretches where the rank should read low.
    """
    ordered = sorted(points)
    if not ordered:
        return IvHistory(source=source)
    current = ordered[-1][1]
    return IvHistory(points=ordered[:-1], source=source, current=current, downloaded=True)


def choose_iv_history(own: IvHistory, vendor: IvHistory) -> IvHistory:
    """Pick one of two vendors' series. Never merge them.

    ## Why this is a choice and not a combination

    The project's hard rule is that an implied vol history belongs to one vendor.
    Two vendors disagree about the mid of a wide contract, about which strikes are
    quoted at all, and about what time of day their number represents, and the
    difference between them is a level shift. A rank is exactly a measure of where a
    value sits inside a range, so splicing a level shift into the range moves the rank
    without the market having moved. Concatenating a downloaded twelve year series to
    eight locally solved sessions would do precisely that, at the join, invisibly.

    ## The rule

    More observations wins, because a rank is only as good as the range it is measured
    against and nothing else here separates the two series in quality. In practice that
    is not close: a Market Chameleon export is roughly 3,185 sessions against the 8
    this project has captured for itself, and the local series will not overtake it
    within the lifetime of the tool.

    The loser is discarded rather than kept as a fallback for missing days, for the
    same reason it is not merged.
    """
    if not vendor:
        return own
    if not own:
        return vendor
    return vendor if len(vendor) >= len(own) else own
