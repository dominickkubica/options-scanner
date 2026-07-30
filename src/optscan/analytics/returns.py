"""Return and capital metrics.

The number that decides whether a trade is worth doing is not the credit, it is the
credit against the capital it ties up and the time it ties it up for. Those two
denominators are where most published "returns" quietly cheat.

## The margin model, stated explicitly

Return on capital means nothing without saying what capital. This module defines it
as follows, and every one of these is an assumption a broker can disagree with:

- Cash secured put: strike * 100, minus the credit received. The cash actually set
  aside. Not the reg-T margin requirement, which is far smaller and turns a 2 percent
  return into a 10 percent one on the same trade.
- Covered call: the stock is the collateral, so capital is the shares' cost basis.
  Passed in, because it is a fact about the position and not about the option.
- Vertical credit spread: width * 100, minus the credit. Max loss, which is what a
  broker holds.
- Iron condor: the wider of the two spread widths * 100, minus the total credit. Only
  one side can lose.
- Naked call: undefined here. There is no bounded loss, so there is no honest
  denominator, and reporting a return on a broker's margin formula would imply a risk
  profile the number does not describe.

Annualizing is done with simple scaling by 365/DTE and not by compounding. Compounding
a 45 day trade into an annual figure assumes you can find eight more like it in a row,
which is a claim about opportunity, not about this trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from optscan.analytics.greeks import DAYS_PER_YEAR

CONTRACT_SIZE = 100


@dataclass(frozen=True, slots=True)
class Commissions:
    """What the broker takes, per contract and per leg.

    Modelled because ignoring it flatters exactly the trades that look best. A one
    point wide credit spread taking 0.21 has 21 dollars of maximum profit, and two
    legs in and two legs out at 0.65 is 2.60 of that, an eighth of the trade. The same
    2.60 against a cash secured put earning 500 dollars is noise. Leaving commissions
    out therefore does not shift every candidate equally, it systematically promotes
    the narrow ones.

    assume_closing_trade is on by default because managing winners early is the whole
    mechanic this tool is built around, and a position closed at 50 percent pays to
    get out. Turn it off to model holding to expiration, where most brokers charge
    nothing for an option that expires worthless.
    """

    per_contract: float = 0.65
    per_trade: float = 0.0
    assume_closing_trade: bool = True

    def cost(self, legs: int, contracts: int = 1) -> float:
        """Total dollars for opening, and closing if that is assumed."""
        if legs < 1 or contracts < 1:
            raise ValueError("a position has at least one leg and one contract")
        opening = legs * contracts * self.per_contract + self.per_trade
        return opening * (2.0 if self.assume_closing_trade else 1.0)


#: No commissions. The explicit default, so a caller that wants gross figures asks
#: for them rather than getting them by omission.
NO_COMMISSIONS = Commissions(per_contract=0.0, per_trade=0.0, assume_closing_trade=False)


@dataclass(frozen=True, slots=True)
class ReturnProfile:
    """What a position pays, against what it risks, over how long."""

    credit: float
    max_profit: float
    max_loss: float | None
    capital: float | None
    dte: int
    commission: float = 0.0

    @property
    def gross_max_profit(self) -> float:
        """Max profit before the broker takes its cut. Kept so the cut is visible."""
        return self.max_profit + self.commission

    @property
    def return_on_capital(self) -> float | None:
        """Max profit over capital committed. Not annualized."""
        if not self.capital or self.capital <= 0:
            return None
        return self.max_profit / self.capital

    @property
    def annualized_return(self) -> float | None:
        """Simple scaling to a year. See the module docstring on why not compounded."""
        roc = self.return_on_capital
        if roc is None or self.dte <= 0:
            return None
        return roc * (DAYS_PER_YEAR / self.dte)

    @property
    def risk_reward(self) -> float | None:
        """Max profit over max loss. Below 1 is normal for short premium and fine.

        Short premium sells a high win rate for a bad payoff ratio. A number here of
        0.25 is not a red flag by itself; it only matters against the win rate, which
        is what Phase 8 measures rather than assumes.
        """
        if self.max_loss is None or self.max_loss <= 0:
            return None
        return self.max_profit / self.max_loss


def cash_secured_put_capital(strike: float, credit: float) -> float:
    """Cash a broker sets aside for a CSP: the full assignment cost, less the credit."""
    if strike <= 0:
        raise ValueError("strike must be positive")
    return strike * CONTRACT_SIZE - credit * CONTRACT_SIZE


def vertical_spread_capital(width: float, credit: float) -> float:
    """Max loss on a credit spread: the width, less the credit taken in."""
    if width <= 0:
        raise ValueError("width must be positive")
    capital = (width - credit) * CONTRACT_SIZE
    if capital <= 0:
        # Credit at or above width. Real when quotes are stale, impossible otherwise,
        # and it would produce an infinite return if allowed through.
        raise ValueError(f"credit {credit} is not below the width {width}, check the quotes")
    return capital


def iron_condor_capital(put_width: float, call_width: float, credit: float) -> float:
    """Only one side of a condor can finish in the money, so capital is the wider wing."""
    return vertical_spread_capital(max(put_width, call_width), credit)


def short_put_profile(
    strike: float,
    credit: float,
    dte: int,
    commissions: Commissions | None = None,
) -> ReturnProfile:
    """Cash secured put.

    Max loss assumes the underlying goes to zero, which is the true bound and is worth
    stating rather than quietly using a percentage move.
    """
    _validate_credit(credit)
    cost = (commissions or NO_COMMISSIONS).cost(legs=1)
    return ReturnProfile(
        credit=credit,
        max_profit=credit * CONTRACT_SIZE - cost,
        max_loss=(strike - credit) * CONTRACT_SIZE + cost,
        capital=cash_secured_put_capital(strike, credit),
        dte=dte,
        commission=cost,
    )


def covered_call_profile(
    strike: float,
    credit: float,
    cost_basis: float,
    dte: int,
    commissions: Commissions | None = None,
) -> ReturnProfile:
    """Covered call against shares held at `cost_basis`.

    Max profit includes the appreciation to the strike, because a covered call called
    away at a strike above basis makes money on the stock as well as the option.
    Reporting only the premium understates a trade that is doing its job.
    """
    _validate_credit(credit)
    if cost_basis <= 0:
        raise ValueError("cost_basis must be positive")
    appreciation = max(strike - cost_basis, 0.0)
    cost = (commissions or NO_COMMISSIONS).cost(legs=1)
    return ReturnProfile(
        credit=credit,
        max_profit=(credit + appreciation) * CONTRACT_SIZE - cost,
        max_loss=(cost_basis - credit) * CONTRACT_SIZE + cost,
        capital=cost_basis * CONTRACT_SIZE,
        dte=dte,
        commission=cost,
    )


def credit_spread_profile(
    short_strike: float,
    long_strike: float,
    credit: float,
    dte: int,
    commissions: Commissions | None = None,
) -> ReturnProfile:
    """Vertical credit spread, either side. Width comes from the strikes."""
    _validate_credit(credit)
    width = abs(short_strike - long_strike)
    cost = (commissions or NO_COMMISSIONS).cost(legs=2)
    return ReturnProfile(
        credit=credit,
        max_profit=credit * CONTRACT_SIZE - cost,
        max_loss=(width - credit) * CONTRACT_SIZE + cost,
        capital=vertical_spread_capital(width, credit),
        dte=dte,
        commission=cost,
    )


def iron_condor_profile(
    put_short: float,
    put_long: float,
    call_short: float,
    call_long: float,
    credit: float,
    dte: int,
    commissions: Commissions | None = None,
) -> ReturnProfile:
    """Iron condor. Capital and max loss both come from the wider wing."""
    _validate_credit(credit)
    put_width = abs(put_short - put_long)
    call_width = abs(call_short - call_long)
    width = max(put_width, call_width)
    cost = (commissions or NO_COMMISSIONS).cost(legs=4)
    return ReturnProfile(
        credit=credit,
        max_profit=credit * CONTRACT_SIZE - cost,
        max_loss=(width - credit) * CONTRACT_SIZE + cost,
        capital=iron_condor_capital(put_width, call_width, credit),
        dte=dte,
        commission=cost,
    )


def credit_to_width(credit: float, width: float) -> float:
    """Credit as a fraction of the spread's width.

    The standard quick filter on a vertical: a third of the width is the usual
    threshold for a spread worth looking at. It is a proxy for whether the risk to
    reward matches the probability, and Phase 3 checks that properly.
    """
    if width <= 0:
        raise ValueError("width must be positive")
    return credit / width


def naked_call_capital() -> None:
    """Deliberately not implemented.

    An uncovered call has unbounded loss, so there is no denominator that honestly
    describes the risk. Returning a broker's margin number here would produce a
    return figure that implies a bounded trade.
    """
    return None


def _validate_credit(credit: float) -> None:
    if credit <= 0:
        raise ValueError(f"credit must be positive for a short premium position, got {credit}")
