"""Management triggers: when a held position wants looking at.

Every trigger here answers "is it time to consider doing something", never "do this".
The distinction is the whole design. A tool that says roll is making a recommendation
it has not earned, and Phase 8 exists precisely because none of this has been validated
against outcomes yet. So a trigger carries a condition, the numbers behind it, and a
sentence, and stops.

## Why these four

They are the ones with a defensible reason rather than the ones that are easy.

- **Profit target.** The one mechanic in premium selling with a real argument behind
  it: the last portion of a credit takes disproportionately long to collect and carries
  the same tail risk throughout, so closing at half pays you most of the credit for a
  fraction of the exposure time. Defaults to 50 percent because that is the convention,
  and it is a convention rather than a finding.
- **Days to expiry.** Gamma rises sharply into the last fortnight, which means the
  position's delta starts moving faster than the underlying does. That is a real change
  in the character of the risk and not a calendar superstition.
- **Delta breach.** A short strike sold at 0.20 delta that now reads 0.40 is a
  different position from the one that was opened, whatever its profit says.
- **Tested.** Spot has crossed a short strike. About the underlying rather than about
  the option's price, and the one most people manage on.

## The one that is deliberately not here

There is no stop loss on a multiple of the credit. It is popular, and on a short option
it systematically closes exactly the positions that were about to recover, because the
loss is largest when the move is largest and the move is what mean reverts. Adding it
would need Phase 8 evidence, and without that it would be this project shipping a rule
it cannot defend.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from optscan.analytics.events import EventWindow, early_assignment_risk
from optscan.models import Right
from optscan.models.opportunity import Action

#: Fraction of maximum profit that counts as taking the trade off. The convention, and
#: labelled as one: nothing in this project has validated it.
DEFAULT_PROFIT_TARGET = 0.50

#: Days to expiry at which gamma starts to dominate. Twenty one is the common choice
#: and the reasoning is real even though the exact number is not special.
DEFAULT_DTE_THRESHOLD = 21

#: Absolute delta on a short leg that counts as a breach. 0.30 is roughly where a
#: strike sold for premium starts behaving like a directional position.
DEFAULT_DELTA_BREACH = 0.30

#: Days before an ex dividend date to start checking assignment on short calls.
DEFAULT_ASSIGNMENT_WINDOW = 5


class TriggerKind(StrEnum):
    PROFIT_TARGET = "profit_target"
    DTE = "dte"
    DELTA_BREACH = "delta_breach"
    TESTED = "tested"
    ASSIGNMENT_RISK = "assignment_risk"
    EARNINGS = "earnings"
    EXPIRED = "expired"


#: How loud each kind is. Used for ordering and for deciding what reaches an alert
#: sink, never for deciding what to do.
SEVERITY: dict[TriggerKind, int] = {
    TriggerKind.EXPIRED: 5,
    TriggerKind.ASSIGNMENT_RISK: 4,
    TriggerKind.TESTED: 3,
    TriggerKind.DELTA_BREACH: 3,
    TriggerKind.EARNINGS: 2,
    TriggerKind.DTE: 2,
    TriggerKind.PROFIT_TARGET: 1,
}


@dataclass(frozen=True, slots=True)
class Trigger:
    """One condition that has become true, with the numbers that made it true."""

    kind: TriggerKind
    message: str
    #: The measured value and the threshold it crossed, so the UI can show the margin
    #: rather than just the verdict. None on triggers with nothing continuous to report.
    value: float | None = None
    threshold: float | None = None

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 0)


def evaluate(
    risk,
    *,
    asof: date,
    events: EventWindow | None = None,
    profit_target: float = DEFAULT_PROFIT_TARGET,
    dte_threshold: int = DEFAULT_DTE_THRESHOLD,
    delta_breach: float = DEFAULT_DELTA_BREACH,
    assignment_window: int = DEFAULT_ASSIGNMENT_WINDOW,
) -> list[Trigger]:
    """Every condition currently true for one position, worst first.

    `risk` is a PositionRisk. Typed loosely to keep this module free of a circular
    import with portfolio.py, which imports nothing from here.

    A condition that cannot be evaluated produces no trigger rather than a false one.
    An unmarked position is not a position at zero profit, and firing a profit target
    on a missing mark would be the worst possible way to be wrong.
    """
    triggers: list[Trigger] = []
    position = risk.position
    dte = position.dte(asof)

    if dte < 0:
        triggers.append(
            Trigger(
                kind=TriggerKind.EXPIRED,
                message=(
                    f"Expired {abs(dte)} days ago and still marked open. Close it so the "
                    "portfolio totals stop counting it."
                ),
                value=float(dte),
                threshold=0.0,
            )
        )

    fraction = risk.profit_fraction
    if fraction is not None and fraction >= profit_target:
        triggers.append(
            Trigger(
                kind=TriggerKind.PROFIT_TARGET,
                message=(
                    f"Holding {fraction:.0%} of maximum profit, at or past the "
                    f"{profit_target:.0%} target. The remaining credit takes longer to "
                    "collect than it did to get here and carries the same tail risk."
                ),
                value=fraction,
                threshold=profit_target,
            )
        )

    if 0 <= dte <= dte_threshold:
        triggers.append(
            Trigger(
                kind=TriggerKind.DTE,
                message=(
                    f"{dte} days to expiry, inside the {dte_threshold} day threshold. "
                    "Gamma rises from here, so delta starts moving faster than spot."
                ),
                value=float(dte),
                threshold=float(dte_threshold),
            )
        )

    breached = _delta_breach(risk, delta_breach)
    if breached is not None:
        strike, measured = breached
        triggers.append(
            Trigger(
                kind=TriggerKind.DELTA_BREACH,
                message=(
                    f"Short {strike:g} now reads {abs(measured):.2f} delta, past the "
                    f"{delta_breach:.2f} threshold. This is a more directional position "
                    "than the one that was opened."
                ),
                value=abs(measured),
                threshold=delta_breach,
            )
        )

    if risk.tested and risk.spot is not None:
        triggers.append(
            Trigger(
                kind=TriggerKind.TESTED,
                message=(
                    f"Spot {risk.spot:.2f} has crossed a short strike. Tested is about "
                    "the underlying, not about what the position is currently worth."
                ),
                value=risk.spot,
            )
        )

    triggers.extend(_event_triggers(risk, asof=asof, events=events, window=assignment_window))
    return sorted(triggers, key=lambda item: (-item.severity, item.kind))


def _delta_breach(risk, threshold: float) -> tuple[float, float] | None:
    """The worst breaching short leg, or None.

    Measured per leg rather than on the position's net delta, because a condor's net
    delta can sit near zero while one side is badly tested, and the net would report
    the book as balanced right up until assignment.
    """
    breaching: tuple[float, float] | None = None
    for item in risk.legs:
        if item.leg.action is not Action.SELL or item.delta is None:
            continue
        if abs(item.delta) < threshold:
            continue
        if breaching is None or abs(item.delta) > abs(breaching[1]):
            breaching = (item.leg.strike, item.delta)
    return breaching


def _event_triggers(
    risk,
    *,
    asof: date,
    events: EventWindow | None,
    window: int,
) -> list[Trigger]:
    """Earnings inside the position's life, and dividend driven assignment risk."""
    if events is None:
        return []

    triggers: list[Trigger] = []
    expiry = risk.position.expiry

    if events.earnings_before(expiry, asof):
        days = events.days_to_earnings(asof)
        triggers.append(
            Trigger(
                kind=TriggerKind.EARNINGS,
                message=(
                    f"Earnings on {events.earnings} lands before the {expiry} expiry, "
                    f"{days} days out. The position will be held through a scheduled move."
                ),
                value=float(days) if days is not None else None,
            )
        )

    days_to_ex_div = events.days_to_ex_dividend(asof)
    for item in risk.legs:
        if item.leg.action is not Action.SELL or item.leg.right is not Right.CALL:
            continue
        if item.mark is None:
            continue
        # Extrinsic, not the mark. A deep in the money call is mostly intrinsic, and it
        # is the sliver of time value left that decides whether exercising is rational.
        intrinsic = max((risk.spot or 0.0) - item.leg.strike, 0.0)
        extrinsic = max(item.mark - intrinsic, 0.0)
        if early_assignment_risk(
            Right.CALL,
            extrinsic,
            events.dividend_amount,
            days_to_ex_div,
            window_days=window,
        ):
            triggers.append(
                Trigger(
                    kind=TriggerKind.ASSIGNMENT_RISK,
                    message=(
                        f"Short {item.leg.strike:g} call has {extrinsic:.2f} of extrinsic "
                        f"left against a {events.dividend_amount:.2f} dividend going ex in "
                        f"{days_to_ex_div} days. Exercising to capture it is rational, so "
                        "somebody will."
                    ),
                    value=extrinsic,
                    threshold=events.dividend_amount,
                )
            )
    return triggers


def worst(triggers: Sequence[Trigger]) -> Trigger | None:
    """The loudest trigger, for a one line summary."""
    return max(triggers, key=lambda item: item.severity) if triggers else None
