"""One module per strategy: csp, covered call, verticals, condors, strangles."""

from optscan.models import Right, Strategy
from optscan.screener.strategies.base import Candidate, StrategyGenerator
from optscan.screener.strategies.singles import CashSecuredPut, CoveredCall
from optscan.screener.strategies.spreads import (
    IronCondor,
    ShortStrangle,
    VerticalCreditSpread,
)

#: Every generator this tool knows about, keyed by the name used in config.
GENERATORS: dict[Strategy, StrategyGenerator] = {
    Strategy.CASH_SECURED_PUT: CashSecuredPut(),
    Strategy.COVERED_CALL: CoveredCall(),
    Strategy.PUT_CREDIT_SPREAD: VerticalCreditSpread(Right.PUT),
    Strategy.CALL_CREDIT_SPREAD: VerticalCreditSpread(Right.CALL),
    Strategy.IRON_CONDOR: IronCondor(),
    Strategy.SHORT_STRANGLE: ShortStrangle(),
}

__all__ = [
    "GENERATORS",
    "Candidate",
    "CashSecuredPut",
    "CoveredCall",
    "IronCondor",
    "ShortStrangle",
    "StrategyGenerator",
    "VerticalCreditSpread",
    "generators_for",
]


def generators_for(names: tuple[str, ...]) -> list[StrategyGenerator]:
    """Resolve configured strategy names to generators.

    An unknown name is an error rather than a silent skip: a typo in the config would
    otherwise mean a strategy the user believes is running simply is not.
    """
    resolved = []
    for name in names:
        try:
            strategy = Strategy(name)
        except ValueError as error:
            known = ", ".join(sorted(s.value for s in Strategy))
            raise ValueError(f"unknown strategy {name!r}. Known strategies: {known}") from error
        resolved.append(GENERATORS[strategy])
    return resolved
