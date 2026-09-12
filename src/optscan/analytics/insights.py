"""Plain-English findings for the journal, one short list per panel.

The panels show numbers; these say what the numbers mean, the way somebody reading the
page would put it. "After a losing day your trades risk 40% more than after a winning
day" is the same fact as two tiles reading $108 and $77, and it is the version that gets
acted on.

## The rules that keep them honest

Every sentence is generated from the data in front of it, and three rules decide whether
a sentence is allowed to exist at all:

- **A comparison needs both sides to be real.** Each group has to hold at least
  `MIN_GROUP` trades before it is compared with another. Below that, "Tuesdays are your
  best day" is one good Tuesday.
- **A sample too small to conclude from says so, in the sentence.** A comparison resting
  on fewer than `MIN_CLUSTERS_FOR_A_CLAIM` independent trades carries "too few to be
  sure", rather than leaving the caveat to a banner somewhere else on the page.
- **An edge is only called an edge when its interval excludes zero.** Otherwise the
  sentence gives the average and says it is not yet distinguishable from nothing.

Nothing here leaves a trade out to make a point: an earlier "exclude the worst day"
view was removed because leaving out only the bad tail can only flatter the record.
These describe what happened. None of them is advice.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence

from optscan.analytics.calibration import MIN_CLUSTERS_FOR_A_CLAIM
from optscan.analytics.journal import Breakdown, JournalReport
from optscan.analytics.positions import (
    EXIT_AFTER,
    EXIT_BY,
    EXIT_RULE_TIME,
    SIZE_FLAG_MULTIPLE,
    Book,
    Position,
)

#: Fewest trades a group needs before it is compared with another.
MIN_GROUP = 5

#: Groups a best-and-worst comparison needs.
BEST_AND_WORST = 2

#: Trades under one tag before its average is worth a sentence.
MIN_TAGGED = 3

#: A relative change in size smaller than this reads as "barely changes".
MEANINGFUL_DIFFERENCE = 0.10

#: A symbol holding at least this share of the trades is worth calling out.
CONCENTRATION = 0.30

#: Win rate this far above break-even reads as comfortable rather than thin.
COMFORTABLE_MARGIN = 0.10

#: Share of the deepest drawdown one day must account for to be named as its cause.
MOSTLY = 0.75
ENTIRELY = 0.99


def _money(value: float, digits: int = 0) -> str:
    # Round before choosing the sign: -0.4 is -$0 otherwise, which reads as a loss.
    value = round(value, digits)
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{digits}f}"


def _plain(value: float) -> str:
    return f"${abs(value):,.0f}"


def _unsure(clusters: int, trades: int) -> str:
    """The caveat a small comparison carries in its own sentence.

    Decided on independent clusters, since two trades closed on the same day in the same
    symbol are one outcome, but worded with the trade count the sentence already shows:
    "over 11 ... though 10 trades is too few" reads as a typo.
    """
    if clusters >= MIN_CLUSTERS_FOR_A_CLAIM:
        return ""
    return f", though {trades} {'trade is' if trades == 1 else 'trades is'} too few to be sure"


def _best_worst(rows: Sequence[Breakdown], label: str) -> str | None:
    """Best and worst group by mean result, when at least two groups are big enough."""
    eligible = [row for row in rows if row.trades >= MIN_GROUP]
    if len(eligible) < BEST_AND_WORST:
        return None
    best = max(eligible, key=lambda row: row.mean_profit)
    worst = min(eligible, key=lambda row: row.mean_profit)
    if best is worst:
        return None
    return (
        f"By {label}, {best.key} has done best ({_money(best.mean_profit, 2)} a trade over "
        f"{best.trades}) and {worst.key} worst ({_money(worst.mean_profit, 2)} over "
        f"{worst.trades})"
        f"{_unsure(min(best.clusters, worst.clusters), min(best.trades, worst.trades))}."
    )


def _grouped(
    positions: Sequence[Position], label: Callable[[Position], str | None]
) -> list[tuple[str, int, float, int]]:
    """(key, trades, total, independent clusters) per label, over closed positions."""
    groups: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        key = label(p)
        if key is not None:
            groups[key].append(p)
    return [
        (
            key,
            len(items),
            sum(p.outcome for p in items),
            len({(p.symbol, p.closed_at) for p in items}),
        )
        for key, items in groups.items()
    ]


# --------------------------------------------------------------------------------
# One function per panel
# --------------------------------------------------------------------------------


def _performance(report: JournalReport, book: Book) -> list[str]:
    out: list[str] = []
    exp = report.expectancy
    if exp is not None:
        if exp.low is None or exp.high is None:
            out.append(
                f"You average {_money(exp.value, 2)} per trade, from too few trades to put "
                "a range on it."
            )
        elif exp.indistinguishable_from_zero:
            out.append(
                f"You average {_money(exp.value, 2)} per trade, but the likely range "
                f"({_money(exp.low)} to {_money(exp.high)}) still includes zero, so it "
                "isn't a proven edge yet."
            )
        else:
            out.append(
                f"You average {_money(exp.value, 2)} per trade, and the likely range "
                f"({_money(exp.low)} to {_money(exp.high)}) sits "
                f"{'above' if exp.low > 0 else 'below'} zero."
            )

    win_rate = report.win_rate
    if win_rate is not None and report.avg_win and report.avg_loss:
        loss = abs(report.avg_loss)
        win = report.avg_win
        breakeven = loss / (win + loss)
        margin = win_rate.value - breakeven
        verdict = (
            "below break-even"
            if margin < 0
            else "a thin margin"
            if margin < COMFORTABLE_MARGIN
            else "a comfortable margin"
        )
        out.append(
            f"You win {win_rate.value:.0%} of trades. With an average loss of {_plain(loss)} "
            f"against an average win of {_plain(win)}, you need about {breakeven:.0%} to "
            f"break even: {verdict}."
        )

    stops = book.stops
    if stops.stopped and stops.mean_multiple is not None:
        past = "none went" if stops.beyond_stop == 0 else f"{stops.beyond_stop} went"
        out.append(
            f"{stops.stopped} trades closed at a loss, averaging {stops.mean_multiple:.2f}x "
            f"the credit; {past} past your {stops.planned_multiple:g}x stop."
        )

    r = book.r_expectancy
    if r is not None and r.low is not None and r.high is not None:
        tail = (
            "which still includes zero"
            if r.indistinguishable_from_zero
            else "above zero"
            if r.low > 0
            else "below zero"
        )
        out.append(
            f"Per unit of risk you average {r.value:+.2f}R "
            f"(range {r.low:+.2f}R to {r.high:+.2f}R, {tail})."
        )
        if r.low > 0 and exp is not None and exp.indistinguishable_from_zero:
            out.append(_size_and_r(book.positions))
    return out


def _size_and_r(positions: Sequence[Position]) -> str:
    """Why R can be proven positive while dollars aren't.

    R counts every trade the same; dollars let the biggest dominate. That gap alone
    says nothing about whether the big trades did worse, so the sentence only says they
    did when trades above the median 1R measurably averaged less R than the rest.
    """
    lead = "Measured in R your trades are positive; in dollars they aren't yet."
    measured = [
        (p.risk_unit, p.r_multiple)
        for p in positions
        if p.risk_unit is not None and p.r_multiple is not None
    ]
    if measured:
        median = statistics.median(risk for risk, _ in measured)
        big = [r for risk, r in measured if risk > median]
        typical = [r for risk, r in measured if risk <= median]
        if min(len(big), len(typical)) >= MIN_GROUP:
            big_r, typical_r = statistics.fmean(big), statistics.fmean(typical)
            if big_r < typical_r:
                return (
                    f"{lead} The difference is size: trades above your median risk averaged "
                    f"{big_r:+.2f}R, the rest {typical_r:+.2f}R."
                )
    return f"{lead} R counts every trade the same, and dollars let the biggest ones dominate."


def _curve(report: JournalReport) -> list[str]:
    out: list[str] = []
    worst = report.worst_day
    best = report.best_day
    total = report.total_profit
    if worst is not None and worst.profit < 0:
        if total > 0 and abs(worst.profit) > total:
            out.append(
                f"Your worst day, {worst.day:%b %d} ({_money(worst.profit)}), lost more than "
                f"your whole total of {_money(total)}."
            )
        else:
            out.append(f"Your worst day was {worst.day:%b %d} ({_money(worst.profit)}).")
        if report.max_drawdown > 0:
            share = abs(worst.profit) / report.max_drawdown
            if share >= ENTIRELY:
                out.append(
                    f"Your deepest drawdown ({_plain(report.max_drawdown)}) was that single day."
                )
            elif share >= MOSTLY:
                out.append(
                    f"Most of your deepest drawdown ({_plain(report.max_drawdown)}) came from "
                    "that one day."
                )
    if best is not None and best.profit > 0:
        out.append(f"Your best day was {best.day:%b %d} ({_money(best.profit)}).")
    return out


def _trades(book: Book) -> list[str]:
    out: list[str] = []
    positions = book.positions
    unnamed = sum(1 for p in positions if not p.confident and p.annotation.strategy is None)
    if unnamed:
        out.append(
            f"{unnamed} positions are labelled multi-leg because their shape couldn't be "
            "named. Tagging them lets the strategy breakdown use them."
        )
    big = sum(1 for p in positions if "sized too big" in p.flags)
    if big:
        out.append(
            f"{big} trades risked more than {SIZE_FLAG_MULTIPLE:g}x your median and are "
            "flagged 'sized too big'."
        )
    past = sum(1 for p in positions if "held past stop" in p.flags)
    if past:
        out.append(f"{past} trades closed well past your stop and are flagged 'held past stop'.")
    untimed = sum(1 for p in positions if p.entry_at is None)
    if untimed:
        verb = "has" if untimed == 1 else "have"
        out.append(f"{untimed} of {len(positions)} positions {verb} no entry time yet.")
    return out


def _breakdowns(book: Book) -> list[str]:
    out: list[str] = []
    closed = [p for p in book.positions if not p.is_open]
    if closed:
        by_symbol = _grouped(closed, lambda p: p.symbol)
        top, count, pnl, _ = max(by_symbol, key=lambda row: row[1])
        share = count / len(closed)
        if share >= CONCENTRATION and len(by_symbol) > 1:
            rest = sum(p.outcome for p in closed) - pnl
            out.append(
                f"{top} is {share:.0%} of your trades and made {_money(pnl)}; everything else "
                f"made {_money(rest)}."
            )

    # Stock has no DTE; beside 0DTE and 8+ DTE it is a different trade, not a slower one.
    dte = [row for row in book.by_dte_class if row.key != "stock"]
    for rows, label in ((book.by_strategy, "strategy"), (dte, "DTE at entry")):
        sentence = _best_worst(rows, label)
        if sentence:
            out.append(sentence)

    days = [
        row
        for row in _grouped([p for p in closed if p.is_option], lambda p: p.weekday)
        if row[1] >= MIN_GROUP
    ]
    if len(days) >= BEST_AND_WORST:
        best = max(days, key=lambda row: row[2] / row[1])
        worst = min(days, key=lambda row: row[2] / row[1])
        if best is not worst:
            out.append(
                f"By day opened, {best[0]} has done best ({_money(best[2] / best[1], 2)} a "
                f"trade over {best[1]}) and {worst[0]} worst ({_money(worst[2] / worst[1], 2)} "
                f"over {worst[1]}){_unsure(min(best[3], worst[3]), min(best[1], worst[1]))}."
            )

    for row in book.by_tag:
        if row.key != "untagged" and row.trades >= MIN_TAGGED:
            out.append(
                f"Trades tagged '{row.key}' averaged {_money(row.mean_profit, 2)} over "
                f"{row.trades}."
            )
    return out


def _time(book: Book) -> list[str]:
    out: list[str] = []
    sentence = _best_worst(book.by_entry_time, "entry time")
    if sentence:
        out.append(sentence)
    rule = f"{EXIT_RULE_TIME:%H:%M} PT"
    by = next((row for row in book.by_exit_time if row.key == EXIT_BY), None)
    after = next((row for row in book.by_exit_time if row.key == EXIT_AFTER), None)
    if by and after and by.win_rate and after.win_rate and min(by.trades, after.trades) > 0:
        out.append(
            f"You closed {by.trades} trades by {rule} and won {by.win_rate.value:.0%} of them; "
            f"{after.trades} closed later and won {after.win_rate.value:.0%}"
            f"{_unsure(min(by.clusters, after.clusters), min(by.trades, after.trades))}."
        )
    elif by and not after:
        out.append(f"Every timed expiry-day close was by {rule}.")
    return out


def _regime(book: Book) -> list[str]:
    out: list[str] = []
    for rows, what in ((book.by_vix, "VIX on the day"), (book.by_trend, "SPY's trend")):
        eligible = [row for row in rows if row.trades >= MIN_GROUP]
        if len(eligible) < BEST_AND_WORST:
            continue
        best = max(eligible, key=lambda row: row.mean_profit)
        worst = min(eligible, key=lambda row: row.mean_profit)
        if best is worst:
            continue
        out.append(
            f"By {what}: trades opened at {best.key} made {_money(best.total_profit)} over "
            f"{best.trades}, at {worst.key} {_money(worst.total_profit)} over {worst.trades}"
            f"{_unsure(min(best.clusters, worst.clusters), min(best.trades, worst.trades))}."
        )
    return out


def _sizing(book: Book) -> list[str]:
    out: list[str] = []
    sizing = book.sizing
    share = sizing.basis == "share"

    def amount(value: float) -> str:
        return f"{value:.1%} of the account" if share else _plain(value)

    if sizing.median_risk:
        out.append(
            f"Your median trade risks {amount(sizing.median_risk)}: 1R, what your stop "
            "puts at risk."
        )
    if (
        sizing.after_win_risk
        and sizing.after_loss_risk
        and min(sizing.after_win_n, sizing.after_loss_n) >= MIN_GROUP
    ):
        change = sizing.after_loss_risk / sizing.after_win_risk - 1
        fewer = min(sizing.after_win_n, sizing.after_loss_n)
        if abs(change) < MEANINGFUL_DIFFERENCE:
            out.append(
                "Your size barely changes after wins or losses "
                f"({amount(sizing.after_win_risk)} vs {amount(sizing.after_loss_risk)})."
            )
        else:
            direction = "more" if change > 0 else "less"
            out.append(
                f"After a losing day your trades risk {abs(change):.0%} {direction} than after "
                f"a winning day ({amount(sizing.after_loss_risk)} vs "
                f"{amount(sizing.after_win_risk)})"
                f"{_unsure(fewer, fewer)}."
            )
    return out


def insights(report: JournalReport, book: Book) -> dict[str, list[str]]:
    """Findings for each journal panel, keyed by the panel they sit under."""
    return {
        "performance": _performance(report, book),
        "curve": _curve(report),
        "trades": _trades(book),
        "breakdowns": _breakdowns(book),
        "time": _time(book),
        "regime": _regime(book),
        "sizing": _sizing(book),
    }
