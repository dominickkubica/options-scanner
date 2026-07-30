"""Event risk: earnings and ex dividend dates.

Pure reasoning about dates and money. Fetching the dates is a provider's job; deciding
what they mean is this module's.

Two things a premium seller needs told, loudly:

- An earnings date inside the expiry means the elevated IV is not an opportunity, it
  is a correctly priced event. Selling it is a bet on the size of the move, which is
  a different trade from selling overpriced vol, whatever the IV rank says.
- A short call goes to early assignment risk around an ex dividend date, and the test
  is not "is it in the money", it is whether the remaining extrinsic value is less
  than the dividend. If it is, exercising to capture the dividend is rational and
  someone will do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from optscan.models import Right


@dataclass(frozen=True, slots=True)
class EventWindow:
    """What lands between now and expiry."""

    earnings: date | None = None
    ex_dividend: date | None = None
    dividend_amount: float | None = None

    def earnings_before(self, expiry: date, asof: date) -> bool:
        """True when an earnings report falls in the life of the position."""
        if self.earnings is None:
            return False
        return asof <= self.earnings <= expiry

    def ex_dividend_before(self, expiry: date, asof: date) -> bool:
        if self.ex_dividend is None:
            return False
        return asof <= self.ex_dividend <= expiry

    def days_to_earnings(self, asof: date) -> int | None:
        return None if self.earnings is None else (self.earnings - asof).days

    def days_to_ex_dividend(self, asof: date) -> int | None:
        return None if self.ex_dividend is None else (self.ex_dividend - asof).days


@dataclass(frozen=True, slots=True)
class EventRisk:
    """The verdict, with the reasons attached."""

    has_earnings: bool
    has_ex_dividend: bool
    days_to_earnings: int | None
    days_to_ex_dividend: int | None
    early_assignment_risk: bool
    reasons: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """No scheduled event and no assignment risk in the life of the position."""
        return not (self.has_earnings or self.early_assignment_risk)


def early_assignment_risk(
    right: Right | str,
    extrinsic_value: float,
    dividend_amount: float | None,
    days_to_ex_dividend: int | None,
    *,
    window_days: int = 5,
) -> bool:
    """Whether a short call is worth exercising early to capture a dividend.

    The rule: a short call is at risk when the dividend exceeds the option's remaining
    extrinsic value, because the holder gives up that extrinsic by exercising and
    receives the dividend instead. Deep in the money short calls just before an ex
    date are the classic case, and moneyness alone does not decide it, the extrinsic
    does.

    Puts are not included. Early exercise of a long put is driven by interest on the
    strike rather than by dividends, and it becomes worth doing on deep in the money
    puts regardless of any event, so it is not an event flag.
    """
    if Right.parse(right) is not Right.CALL:
        return False
    if dividend_amount is None or dividend_amount <= 0:
        return False
    if days_to_ex_dividend is None or not 0 <= days_to_ex_dividend <= window_days:
        return False
    return extrinsic_value < dividend_amount


def assess_events(
    events: EventWindow,
    expiry: date,
    asof: date,
    *,
    right: Right | str | None = None,
    extrinsic_value: float | None = None,
    assignment_window_days: int = 5,
) -> EventRisk:
    """Everything scheduled between now and expiry, and what it implies."""
    reasons: list[str] = []

    has_earnings = events.earnings_before(expiry, asof)
    days_to_earnings = events.days_to_earnings(asof)
    if has_earnings:
        reasons.append(
            f"earnings on {events.earnings} falls before the {expiry} expiry, "
            "so elevated IV here is priced, not mispriced"
        )

    has_ex_div = events.ex_dividend_before(expiry, asof)
    days_to_ex_div = events.days_to_ex_dividend(asof)
    if has_ex_div:
        reasons.append(f"ex dividend on {events.ex_dividend} falls before expiry")

    assignment = False
    if right is not None and extrinsic_value is not None:
        assignment = early_assignment_risk(
            right,
            extrinsic_value,
            events.dividend_amount,
            days_to_ex_div,
            window_days=assignment_window_days,
        )
        if assignment:
            reasons.append(
                f"short call has {extrinsic_value:.2f} of extrinsic against a "
                f"{events.dividend_amount:.2f} dividend, so early assignment is rational"
            )

    return EventRisk(
        has_earnings=has_earnings,
        has_ex_dividend=has_ex_div,
        days_to_earnings=days_to_earnings,
        days_to_ex_dividend=days_to_ex_div,
        early_assignment_risk=assignment,
        reasons=tuple(reasons),
    )


def excludes_earnings(
    events: EventWindow,
    expiry: date,
    asof: date,
    *,
    buffer_days: int = 0,
) -> bool:
    """True when the position closes clear of earnings, with an optional buffer.

    The buffer extends the exclusion past expiry, for people who do not want to be
    holding into the week of a report even if the position technically expires first.
    """
    if events.earnings is None:
        return True
    return not (asof <= events.earnings <= expiry + timedelta(days=buffer_days))
