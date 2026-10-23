"""The forward test of the bounce's spread plan: real fills against the plan they came from.

The call debit spread on an oversold bounce is a lead, not a validated trade: it looked
strong in the backtest and missed its pre-registered gate, and the backtest itself says
the fill decides whether it makes money. The only evidence that can settle it is trades
taken for real, so every position opened from a plan (`optscan position add --from-idea`)
carries that plan, and this report reads them back:

- **The fill against the plan.** The debit paid against the plan's mid, limit and
  walk-away. The study assumed 5% over mid and lost the edge near 15%.
- **The timing against the plan.** Entered on the planned session or later, exited at
  the planned close or not. A trade that deviates is still reported, and labelled, so
  the record says which trades were the tested trade.
- **The outcome.** Realized return on the debit for closed trades, after commissions.

Open positions are listed without a mark: this reads the database and nothing else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from optscan.config import Settings
from optscan.models.opportunity import Strategy
from optscan.models.position import Position
from optscan.storage import db
from optscan.storage import positions as store

#: The slippage the study assumed, and where it found the edge gone. Quoted on the
#: report so a fill can be read against them without looking anything up.
STUDY_SLIPPAGE = 0.05
EDGE_GONE_SLIPPAGE = 0.15

#: A spread needs two returns before it has one.
MIN_FOR_ERROR = 2


@dataclass(frozen=True, slots=True)
class ForwardTrade:
    position: Position
    debit: float
    plan_mid: float | None
    slippage: float | None
    entered_late: bool | None
    exit_status: str
    return_on_debit: float | None


def _per_spread(position: Position) -> float:
    contracts = max(leg.quantity for leg in position.legs)
    size = position.legs[0].contract_size
    return -position.entry_credit / (contracts * size)


def _exit_status(position: Position) -> str:
    if position.is_open:
        return "open"
    if position.exit_on is None or position.closed_at is None:
        return "closed"
    closed = position.closed_at.date()
    if closed == position.exit_on:
        return "closed on plan"
    return "closed early" if closed < position.exit_on else "closed late"


def trade(position: Position) -> ForwardTrade:
    plan = position.plan or {}
    debit = _per_spread(position)
    mid = plan.get("debit")
    entry = plan.get("entry_session")
    paid = -position.net_credit
    return ForwardTrade(
        position=position,
        debit=debit,
        plan_mid=float(mid) if mid else None,
        slippage=debit / float(mid) - 1 if mid else None,
        entered_late=(
            position.opened_at.date() > date.fromisoformat(str(entry)) if entry else None
        ),
        exit_status=_exit_status(position),
        return_on_debit=(
            position.realized / paid if position.realized is not None and paid > 0 else None
        ),
    )


def forward_trades(settings: Settings) -> list[ForwardTrade]:
    """Every position opened from a plan or labelled a call debit spread, oldest first."""
    with db.session(settings.sqlite_path) as conn:
        held = store.list_positions(conn, status=None)
    chosen = [p for p in held if p.plan is not None or p.strategy == Strategy.CALL_DEBIT_SPREAD]
    return [trade(p) for p in sorted(chosen, key=lambda p: p.opened_at)]


@dataclass(frozen=True, slots=True)
class Summary:
    closed: int
    on_plan: int
    mean_return: float | None
    standard_error: float | None
    win_rate: float | None
    total: float
    mean_slippage: float | None


def summarize(trades: list[ForwardTrade]) -> Summary:
    returns = [t.return_on_debit for t in trades if t.return_on_debit is not None]
    slips = [t.slippage for t in trades if t.slippage is not None]
    mean = sum(returns) / len(returns) if returns else None
    error = None
    if len(returns) >= MIN_FOR_ERROR and mean is not None:
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        error = math.sqrt(variance / len(returns))
    return Summary(
        closed=len(returns),
        on_plan=sum(
            1 for t in trades if t.exit_status == "closed on plan" and t.entered_late is False
        ),
        mean_return=mean,
        standard_error=error,
        win_rate=sum(1 for r in returns if r > 0) / len(returns) if returns else None,
        total=sum(t.position.realized or 0.0 for t in trades),
        mean_slippage=sum(slips) / len(slips) if slips else None,
    )


def report_lines(trades: list[ForwardTrade]) -> list[str]:
    if not trades:
        return [
            "No forward-test trades yet. Record one with "
            "`optscan position add ... --from-idea` when you take a plan."
        ]
    lines = [
        f"{'id':>3}  {'symbol':<6} {'opened':<10} {'paid':>6} {'plan mid':>8} {'vs mid':>7}  "
        f"{'entry':<8} {'exit':<15} {'return':>7}"
    ]
    for t in trades:
        p = t.position
        lines.append(
            f"{p.id:>3}  {p.symbol:<6} {p.opened_at.date()!s:<10} {t.debit:>6.2f} "
            f"{t.plan_mid if t.plan_mid is not None else float('nan'):>8.2f} "
            f"{'' if t.slippage is None else f'{t.slippage:+.0%}':>7}  "
            f"{'-' if t.entered_late is None else ('late' if t.entered_late else 'on plan'):<8} "
            f"{t.exit_status:<15} "
            f"{'' if t.return_on_debit is None else f'{t.return_on_debit:+.0%}':>7}"
        )
    s = summarize(trades)
    lines.append("")
    if s.closed:
        spread = f" (+/- {s.standard_error:.0%})" if s.standard_error is not None else ""
        lines.append(
            f"{s.closed} closed: mean {s.mean_return:+.0%} on the debit{spread}, "
            f"{s.win_rate:.0%} won, {s.total:+.2f} in total; "
            f"{s.on_plan} entered and exited exactly on plan."
        )
    else:
        lines.append("Nothing closed yet.")
    if s.mean_slippage is not None:
        lines.append(
            f"Fills averaged {s.mean_slippage:+.0%} against the plan's mid. The study "
            f"assumed {STUDY_SLIPPAGE:+.0%} and lost the edge near {EDGE_GONE_SLIPPAGE:+.0%}."
        )
    lines.append(
        "A handful of trades says almost nothing: in the backtest about half won and the "
        "edge came from the size of the wins, so it takes dozens before the mean means much."
    )
    return lines
