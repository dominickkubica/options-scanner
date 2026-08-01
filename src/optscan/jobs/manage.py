"""The management run: mark every held position, evaluate it, and alert once.

The counterpart to the snapshot job. That one writes history; this one reads it and
asks whether anything wants attention.

Safe to run repeatedly, which is the point. Alert suppression is in the database, so a
scheduled run every fifteen minutes sends each condition once rather than sixty times a
day, and a restart does not reset that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from optscan.alerts import Alert, AlertSink, default_sinks, raise_alerts
from optscan.analytics.events import EventWindow
from optscan.analytics.portfolio import Beta, PortfolioRisk, PositionRisk, beta
from optscan.analytics.triggers import Trigger, evaluate
from optscan.config import Settings
from optscan.jobs.load import latest_snapshot
from optscan.logging import get_logger
from optscan.models import PriceBar
from optscan.models.position import PositionStatus
from optscan.providers import MarketDataProvider, ProviderError
from optscan.screener.config import ScreenConfig
from optscan.screener.context import SymbolAnalysis, analyze_snapshot
from optscan.screener.positions import MarkConvention, build_portfolio
from optscan.storage import db
from optscan.storage import positions as store

log = get_logger("optscan.jobs.manage")

#: Sessions of history pulled per symbol for the beta regression. Two years is enough
#: to clear the sixty session minimum comfortably and short enough to describe a
#: relationship that still holds.
BETA_HISTORY_DAYS = 730


@dataclass(frozen=True, slots=True)
class ManageResult:
    """What one run found."""

    portfolio: PortfolioRisk
    triggers: dict[int, list[Trigger]] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> int:
        return sum(1 for items in self.triggers.values() if items)


def run_manage(
    settings: Settings,
    config: ScreenConfig,
    *,
    provider: MarketDataProvider | None = None,
    sinks: list[AlertSink] | None = None,
    asof: date | None = None,
    send_alerts: bool = True,
    convention: MarkConvention = MarkConvention.CLOSING,
    reference: str = "SPY",
) -> ManageResult:
    """Mark every open position, evaluate triggers, and deliver new alerts.

    `provider` is optional and only buys two things: the beta regression and the
    corporate calendar. Without it the run still marks positions and fires every
    trigger that does not need an event date, and says which ones it could not check.
    """
    today = asof or datetime.now(UTC).date()
    notes: list[str] = []

    with db.session(settings.sqlite_path) as conn:
        held = store.list_positions(conn, status=PositionStatus.OPEN)
        if not held:
            return ManageResult(
                portfolio=PortfolioRisk(positions=(), asof=today, reference=reference),
                notes=["No open positions."],
            )

        symbols = sorted({position.symbol for position in held})
        analyses = _load_analyses(settings, symbols, notes)
        betas = _load_betas(provider, symbols, reference, notes)
        events = _load_events(provider, symbols, notes)

        risks, portfolio_notes = build_portfolio(
            held,
            analyses,
            asof=today,
            betas=betas,
            convention=convention,
            reference=reference,
        )
        notes.extend(portfolio_notes)

        triggers: dict[int, list[Trigger]] = {}
        alerts: list[Alert] = []
        sink_list = sinks if sinks is not None else default_sinks(settings.data_path)

        for risk in risks:
            identifier = risk.position.id
            if identifier is None:
                continue
            fired = evaluate(
                risk,
                asof=today,
                events=events.get(risk.position.symbol),
                profit_target=config.management.profit_target,
                dte_threshold=config.management.dte_threshold,
                delta_breach=config.management.delta_breach,
            )
            triggers[identifier] = fired
            if fired and send_alerts:
                alerts.extend(
                    raise_alerts(
                        conn,
                        identifier,
                        risk.position.symbol,
                        fired,
                        sink_list,
                        min_severity=config.management.alert_min_severity,
                    )
                )

    portfolio = PortfolioRisk(
        positions=tuple(risks),
        asof=today,
        reference=reference,
        notes=tuple(notes),
    )
    log.info(
        "management run complete",
        positions=len(risks),
        flagged=sum(1 for items in triggers.values() if items),
        alerts=len(alerts),
    )
    return ManageResult(portfolio=portfolio, triggers=triggers, alerts=alerts, notes=notes)


def _load_analyses(
    settings: Settings,
    symbols: list[str],
    notes: list[str],
) -> dict[str, SymbolAnalysis | None]:
    """Solve the most recent stored snapshot for every symbol held."""
    analyses: dict[str, SymbolAnalysis | None] = {}
    for symbol in symbols:
        snapshot = latest_snapshot(settings.snapshot_path, symbol)
        if snapshot is None:
            analyses[symbol] = None
            continue
        try:
            analyses[symbol] = analyze_snapshot(snapshot, rate=settings.risk_free_rate)
        except ValueError as error:
            analyses[symbol] = None
            notes.append(f"{symbol} could not be solved: {error}")
    return analyses


def _load_betas(
    provider: MarketDataProvider | None,
    symbols: list[str],
    reference: str,
    notes: list[str],
) -> dict[str, Beta]:
    """Regress each symbol's returns on the reference's.

    The reference is fetched once and reused. Without a provider this returns nothing
    and the portfolio simply withholds its beta weighted delta, which is the honest
    outcome rather than defaulting every beta to one.
    """
    if provider is None:
        notes.append(
            "No provider available, so beta weighted delta was not computed. It needs "
            "price history for each symbol and for the reference."
        )
        return {}

    reference_bars = _history(provider, reference)
    if not reference_bars:
        notes.append(f"No price history for {reference}, so nothing could be beta weighted.")
        return {}

    betas: dict[str, Beta] = {}
    for symbol in symbols:
        if symbol == reference:
            # A symbol regressed on itself is 1.0 by construction, and running the fit
            # would report a spuriously perfect r squared as though it were evidence.
            betas[symbol] = Beta(
                value=1.0,
                observations=len(reference_bars),
                span_days=BETA_HISTORY_DAYS,
                r_squared=1.0,
                reference=reference,
            )
            continue
        bars = _history(provider, symbol)
        if not bars:
            continue
        result = beta(bars, reference_bars, reference=reference)
        if result is not None:
            betas[symbol] = result
    return betas


def _history(provider: MarketDataProvider, symbol: str) -> list[PriceBar]:
    try:
        return provider.get_history(symbol, BETA_HISTORY_DAYS)
    except (ProviderError, OSError, ValueError) as error:
        log.warning("history unavailable", symbol=symbol, error=str(error))
        return []


def _load_events(
    provider: MarketDataProvider | None,
    symbols: list[str],
    notes: list[str],
) -> dict[str, EventWindow]:
    """Corporate calendars, for the earnings and assignment triggers.

    A provider without a calendar, which Tradier is, means those two triggers do not
    fire. Said out loud rather than left as a silent gap: "no assignment warning" and
    "assignment was never checked" are very different states to be in.
    """
    if provider is None:
        notes.append(
            "No provider available, so earnings and dividend assignment triggers were "
            "not checked. Their absence below means unchecked, not clear."
        )
        return {}

    windows: dict[str, EventWindow] = {}
    unavailable: list[str] = []
    for symbol in symbols:
        try:
            events = provider.get_events(symbol)
        except NotImplementedError:
            notes.append(
                "The configured provider has no corporate calendar, so earnings and "
                "dividend assignment triggers were not checked."
            )
            return {}
        except (ProviderError, OSError) as error:
            unavailable.append(symbol)
            log.warning("event calendar unavailable", symbol=symbol, error=str(error))
            continue
        windows[symbol] = EventWindow(
            earnings=events.earnings_date,
            ex_dividend=events.ex_dividend_date,
            dividend_amount=events.dividend_amount,
        )

    if unavailable:
        notes.append(
            f"Earnings and assignment were not checked for {', '.join(unavailable)}: "
            "the calendar could not be reached."
        )
    return windows


def summarize(result: ManageResult) -> list[str]:
    """One line per position, for a terminal."""
    lines: list[str] = []
    for risk in result.portfolio.positions:
        lines.append(_position_line(risk, result.triggers.get(risk.position.id or -1, [])))
    return lines


def _position_line(risk: PositionRisk, triggers: list[Trigger]) -> str:
    position = risk.position
    profit = risk.unrealized
    profit_text = f"{profit:+.0f}" if profit is not None else "unmarked"
    fraction = risk.profit_fraction
    fraction_text = f" ({fraction:.0%} of max)" if fraction is not None else ""
    flags = f"  [{', '.join(str(item.kind) for item in triggers)}]" if triggers else ""
    return (
        f"{position.id:>4}  {position.symbol:<6} {position.expiry}  "
        f"{risk.dte:>4}d  {profit_text:>10}{fraction_text}{flags}"
    )
