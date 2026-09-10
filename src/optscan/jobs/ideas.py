"""Trade ideas: strategies that survived validation, and what is triggering them today.

## The rule this module exists to enforce

**Nothing appears here that has not survived a documented test, and every idea carries
that test's numbers with it.** A screen that lists what is firing today is trivial to
build and worthless without the second half, because the interesting question is never
"is this condition true" but "does this condition being true mean anything".

So `ValidatedStrategy` cannot be constructed without `Evidence`, and `Evidence` records
where the strategy was tested, on how many independent blocks, at what p-value, and what
it was charged to trade. The registry is short by design: on 2026-09-08 a search over 184
combinations produced nothing that survived a holdout, and one pre-specified hypothesis
did. One entry is the honest size of this list.

## Why "supported" is the strongest word available

None of these is validated in the sense that matters, which is forward performance on
money. They have survived a backtest that tries hard not to lie, on one market, one
decade, and a survivorship-biased universe. `Status.SUPPORTED` means the evidence held up
when it was pushed; it does not mean the edge will persist, and the moment one of these
is traded it starts being tested for real.

A strategy that stops working belongs in `RETIRED` with the reason, not deleted. The
record of what was believed and why is the only thing that makes the next search less
credulous than the last.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from optscan.analytics import rules as rule_registry
from optscan.config import Settings
from optscan.jobs.backtest import Strategy, load_series, symbol_costs
from optscan.jobs.signals import MAX_STALE_DAYS, newest_session
from optscan.logging import get_logger
from optscan.storage import db

log = get_logger("optscan.jobs.ideas")

#: How recent a trigger has to be to be worth showing. The rules here act on the next
#: open, so a trigger from last week is a trade that has already been missed.
DEFAULT_FRESHNESS_DAYS = 3

#: Sessions of history this screen reads per symbol. Enough for the longest indicator
#: warmup in the registry several times over, and a small fraction of what is stored:
#: reading everything took thirteen seconds of a fifteen second request, which made the
#: panel look broken rather than slow.
#:
#: It also changes the cost estimate, and for the better. A spread averaged over ten
#: years describes a symbol's past liquidity; a trade taken tomorrow pays this year's.
LOOKBACK_SESSIONS = 300

#: A symbol must keep at least this much of the edge after its own round trip cost to be
#: listed. Not zero, for two reasons.
#:
#: The gross edge is an *estimate* with a confidence interval, and the cost estimate is
#: a Corwin-Schultz approximation that understates on thin names rather than overstating.
#: A row at exactly break-even is therefore more likely to be negative than positive.
#: Ten basis points is roughly a fifth of the surviving rule's net return, which is a
#: thin enough margin to keep the honest names and thick enough to drop the coin flips.
MIN_NET_EDGE = 0.0010


class Status(StrEnum):
    #: Survived a pre-specified out-of-sample test. The strongest word available here.
    SUPPORTED = "supported"
    #: Promising in sample, not yet confirmed anywhere it had not already been fitted.
    PROVISIONAL = "provisional"
    #: Stopped working, or failed a holdout. Kept with the reason, never deleted.
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class Evidence:
    """What was actually measured, and where. No idea ships without one of these."""

    #: The edge over a null that enters at the same frequency at other times.
    edge: float
    p_value: float
    #: Independent calendar blocks, not trades. The real sample size.
    blocks: int
    trades: int
    #: Mean net return per trade after the costs described below.
    net_return: float
    #: What population the test ran on, in words. "188 symbols outside the tech group",
    #: not "the universe": which symbols is the whole point of a confirmation.
    tested_on: str
    tested_at: date
    #: How trading was charged. A result at ten basis points that dies at fifty is not a
    #: result, and this field is what lets a reader check.
    cost_basis: str
    caveats: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "edge": self.edge,
            "p_value": self.p_value,
            "blocks": self.blocks,
            "trades": self.trades,
            "net_return": self.net_return,
            "tested_on": self.tested_on,
            "tested_at": self.tested_at.isoformat(),
            "cost_basis": self.cost_basis,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True, slots=True)
class ValidatedStrategy:
    key: str
    label: str
    #: Why it might work, in one sentence. A rule with no story is a fitted parameter,
    #: and knowing the story is what lets somebody notice when it stops applying.
    rationale: str
    strategy: Strategy
    evidence: Evidence
    status: Status = Status.PROVISIONAL

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "rationale": self.rationale,
            "status": self.status.value,
            "strategy": self.strategy.to_dict(),
            "evidence": self.evidence.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Idea:
    """One symbol currently triggering one validated strategy."""

    symbol: str
    strategy_key: str
    label: str
    session: date
    price: float
    #: Sessions since the trigger. Zero is today's close, one is yesterday's.
    age: int
    #: This symbol's own estimated round trip cost, which is the number that decides
    #: whether the edge is worth anything on this particular name.
    cost: float
    #: The strategy's measured gross edge per trade, before this symbol's costs. Carried
    #: on the idea so `net_edge` is a fact about this row rather than a lookup the
    #: caller has to remember to perform.
    gross_edge: float
    horizon: int
    direction: str

    @property
    def net_edge(self) -> float:
        """What this signal is worth **on this symbol**, after its own round trip cost.

        The gross edge is a property of the rule and is the same everywhere. The cost is
        a property of the symbol and varies by a factor of four across the universe. So
        this is the only number on the row that answers "is this trade worth taking",
        and until 2026-09-10 it was computed, printed, and then ignored: the list was
        sorted by cost and every triggering symbol was shown regardless of whether its
        own spread had already eaten the whole edge.
        """
        return self.gross_edge - self.cost

    @property
    def edge_after_cost_note(self) -> str:
        return f"{self.cost * 10_000:.0f} bp round trip on this symbol"

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy_key,
            "label": self.label,
            "session": self.session.isoformat(),
            "price": self.price,
            "age": self.age,
            "cost_bp": self.cost * 10_000,
            "gross_edge_bp": self.gross_edge * 10_000,
            "net_edge_bp": self.net_edge * 10_000,
            "horizon": self.horizon,
            "direction": self.direction,
        }


# --------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------

#: Everything that has been tested and is worth listing. Short on purpose.
#:
#: A 184 combination search over rules, parameters, horizons and directions on the liquid
#: universe produced nothing that survived its holdout: the in-sample winner,
#: `gap_down(0.04) short 1d`, scored a z of 7.70 against a multiplicity bar of 3.23 and
#: then returned -0.34% out of sample. Every finalist reversed sign. That is recorded in
#: RETIRED rather than forgotten.
REGISTRY: tuple[ValidatedStrategy, ...] = (
    ValidatedStrategy(
        key="oversold_bounce",
        label="Oversold bounce (RSI below 30, held five sessions)",
        rationale=(
            "Short-term reversal: forced or impatient selling pushes price below where "
            "the marginal buyer would have it, and the gap closes over the following "
            "week. One of the oldest and most replicated effects in equities."
        ),
        strategy=Strategy(
            name="oversold_bounce",
            entry="rsi_below",
            entry_params={"threshold": 30},
            horizon=5,
            direction="long",
            cost_model="estimated",
        ),
        evidence=Evidence(
            edge=0.0071,
            p_value=0.004,
            blocks=118,
            trades=4045,
            net_return=0.0046,
            tested_on=(
                "the 188 symbols outside the tech group, which the search that "
                "produced the hypothesis never saw"
            ),
            tested_at=date(2026, 9, 8),
            cost_basis=(
                "each symbol's own Corwin-Schultz spread, median 47 basis points a "
                "round trip across that population"
            ),
            caveats=(
                "The edge is measured against entering at the same frequency at other "
                "times, so it is a timing result and not a promise of profit.",
                "On the liquid subset alone the same rule nets +1.03% at p=0.001, and "
                "on the thinnest names the 150 to 250 basis point spread eats it "
                "entirely. Where it is traded matters more than whether it is traded.",
                "The universe is today's index membership, so the sample excludes the "
                "companies that did not survive to 2026.",
                "One market, one decade. Nothing here has been traded with money.",
            ),
        ),
        status=Status.SUPPORTED,
    ),
    ValidatedStrategy(
        key="gap_down_continuation",
        label="Short a four percent gap down (retired)",
        rationale=(
            "Gaps were thought to continue rather than fill, so a sharp gap down was "
            "followed by more weakness."
        ),
        strategy=Strategy(
            name="gap_down_continuation",
            entry="gap_down",
            entry_params={"size": 0.04},
            horizon=1,
            direction="short",
            cost_model="estimated",
        ),
        evidence=Evidence(
            edge=0.0184,
            p_value=0.132,
            blocks=39,
            trades=0,
            net_return=-0.0034,
            tested_on="123 liquid symbols, trained to 2023-05-17 and held out after",
            tested_at=date(2026, 9, 8),
            cost_basis="each symbol's own estimated spread",
            caveats=(
                "Scored a z of 7.70 in sample against a multiplicity bar of 3.23, which "
                "is exactly the profile of a discovery, and then returned -0.34% out of "
                "sample. Its two sibling cells returned -1.70% and -1.23%.",
                "Kept here as the clearest example this project has of an in-sample "
                "winner that was nothing. Gaps continued until roughly 2023 and have "
                "filled since.",
            ),
        ),
        status=Status.RETIRED,
    ),
)


def supported() -> list[ValidatedStrategy]:
    """Only the strategies worth acting on. Retired and provisional ones are excluded."""
    return [item for item in REGISTRY if item.status is Status.SUPPORTED]


def by_key(key: str) -> ValidatedStrategy | None:
    return next((item for item in REGISTRY if item.key == key), None)


# --------------------------------------------------------------------------------
# What is triggering now
# --------------------------------------------------------------------------------


def find_ideas(
    settings: Settings,
    symbols: Sequence[str] | None = None,
    *,
    freshness: int = DEFAULT_FRESHNESS_DAYS,
    include_provisional: bool = False,
) -> tuple[list[Idea], list[str]]:
    """Symbols currently triggering a validated strategy, and any notes for the reader.

    Stale symbols are skipped for the same reason the signal scan skips them: a delisted
    ticker's last session triggers forever and would sit at the top of this list until
    somebody noticed. The backtester keeps those symbols because their history is real;
    a list of things to trade today must not.
    """
    strategies = supported()
    if include_provisional:
        strategies += [s for s in REGISTRY if s.status is Status.PROVISIONAL]
    if not strategies:
        return [], ["No strategy has passed validation, so there is nothing to list."]

    if symbols is None:
        with db.session(settings.sqlite_path) as conn:
            symbols = [
                row[0]
                for row in conn.execute("SELECT DISTINCT symbol FROM vendor_daily ORDER BY symbol")
            ]

    series, _ = load_series(settings, symbols, lookback=LOOKBACK_SESSIONS)
    if not series:
        return [], ["No symbol has enough stored history to evaluate."]

    with db.session(settings.sqlite_path) as conn:
        asof = newest_session(conn)

    notes: list[str] = []
    stale = 0
    ideas: list[Idea] = []

    for item in strategies:
        rule = rule_registry.resolve(item.strategy.entry, item.strategy.entry_params)
        costs = symbol_costs(item.strategy, series)

        for symbol, bars in series.items():
            last = bars[-1].ts.date()
            if asof is not None and (asof - last).days > MAX_STALE_DAYS:
                stale += 1
                continue

            fired = rule(bars)
            if not fired:
                continue
            # Only the most recent trigger matters: these rules act on the next open, so
            # an older one is a trade that has already been and gone.
            latest = fired[-1]
            age = len(bars) - 1 - latest
            if age > freshness:
                continue

            ideas.append(
                Idea(
                    symbol=symbol,
                    strategy_key=item.key,
                    label=item.label,
                    session=bars[latest].ts.date(),
                    price=bars[latest].close,
                    age=age,
                    cost=costs.get(symbol, item.strategy.cost),
                    gross_edge=item.evidence.edge,
                    horizon=item.strategy.horizon,
                    direction=item.strategy.direction,
                )
            )

    # Drop the symbols whose own spread has already eaten the edge.
    #
    # This is not a liquidity preference, it is arithmetic. The oversold rule is worth
    # +0.71% gross; TROX costs 1.51% to round trip. Listing it as a trade idea asserts a
    # positive expectation that the strategy's own measured numbers deny. The evidence
    # says so directly -- "on the thinnest names the 150 to 250 basis point spread eats
    # it" -- and the rule was validated on liquid symbols in the first place, so a thin
    # name is also outside the population the p-value was earned on.
    #
    # Named rather than silently filtered: a symbol vanishing with no explanation is how
    # somebody concludes the scanner is broken and stops trusting the ones it does show.
    priced_out = [idea for idea in ideas if idea.net_edge < MIN_NET_EDGE]
    ideas = [idea for idea in ideas if idea.net_edge >= MIN_NET_EDGE]

    # Best net edge first. Sorting by cost ranked the cheapest symbol top, which is the
    # right direction and the wrong quantity: what the reader wants is what the trade is
    # worth here, and that is gross minus this symbol's own cost.
    ideas.sort(key=lambda idea: (idea.age, -idea.net_edge))

    if priced_out:
        worst = ", ".join(
            f"{idea.symbol} ({idea.cost * 10_000:.0f}bp)"
            for idea in sorted(priced_out, key=lambda i: -i.cost)[:5]
        )
        notes.append(
            f"{len(priced_out)} triggering symbols are not listed because their own "
            f"round trip cost leaves less than {MIN_NET_EDGE * 10_000:.0f}bp of the "
            f"edge: {worst}. The signal fired on them; the trade is not worth taking."
        )

    if stale:
        notes.append(f"{stale} symbol checks were skipped for stale price history.")
    if not ideas:
        notes.append(
            f"Nothing has triggered in the last {freshness} sessions. That is the "
            "normal state: the supported rule fires on a few percent of symbol-days."
        )

    log.info("trade ideas", strategies=len(strategies), ideas=len(ideas))
    return ideas, notes


def as_report(
    ideas: Sequence[Idea], notes: Sequence[str], *, now: datetime | None = None
) -> dict[str, object]:
    """The whole surface as plain data, evidence included.

    The evidence travels with the ideas rather than being available on request. A list of
    tickers with no numbers attached is exactly the artefact this module exists to avoid
    producing.
    """
    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "ideas": [idea.as_dict() for idea in ideas],
        "strategies": [item.as_dict() for item in REGISTRY],
        "notes": list(notes),
    }
