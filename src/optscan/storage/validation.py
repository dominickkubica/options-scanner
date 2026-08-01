"""Reading and writing the validation log.

The log is append only in spirit. `record_scan` writes candidates and nothing rewrites
them, because a resolved outcome has to stay attached to the score the candidate was
actually given at the time. Re-scoring history under changed weights is a legitimate
and different question, and answering it must not overwrite the original.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from optscan.models import Opportunity, Right
from optscan.models.opportunity import Action


@dataclass(frozen=True, slots=True)
class LoggedOpportunity:
    """A scored candidate as it was recorded, plus its outcome if it has one."""

    id: int
    scan_id: int
    recorded_at: datetime
    session_date: date
    symbol: str
    strategy: str
    expiry: date
    dte: int
    underlying_price: float
    short_strike: float | None
    short_right: str | None
    width: float | None
    credit: float
    max_profit: float
    max_loss: float | None
    probability_of_profit: float | None
    short_delta: float | None
    iv: float | None
    iv_rank: float | None
    liquidity_score: float | None
    score: float
    # Outcome, when resolved.
    outcome: str | None = None
    settlement_price: float | None = None
    finished_beyond: bool | None = None
    profit: float | None = None
    profit_fraction: float | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome is not None

    @property
    def won(self) -> bool | None:
        """Whether the position made money. None until resolved.

        Money rather than "expired worthless", because a short put assigned a cent in
        the money still keeps almost all of the credit, and calling that a loss would
        mismeasure exactly the cases the probability model is being tested on.
        """
        return None if self.profit is None else self.profit > 0


def _short_leg(opportunity: Opportunity):
    """The short leg nearest the money, which is what an outcome turns on."""
    shorts = [leg for leg in opportunity.legs if leg.action is Action.SELL]
    if not shorts:
        return None
    return max(shorts, key=lambda leg: leg.strike if leg.right is Right.PUT else -leg.strike)


def _width(opportunity: Opportunity) -> float | None:
    """Distance between the short and long strike on the tested side, if there is one."""
    short = _short_leg(opportunity)
    if short is None:
        return None
    longs = [
        leg for leg in opportunity.legs if leg.action is Action.BUY and leg.right is short.right
    ]
    if not longs:
        return None
    nearest = min(longs, key=lambda leg: abs(leg.strike - short.strike))
    return abs(short.strike - nearest.strike)


def start_scan(
    conn: sqlite3.Connection,
    *,
    session_date: date,
    symbols: list[str],
    config_digest: str | None = None,
    note: str | None = None,
) -> int:
    """Open a scan run and return its id.

    Every candidate from one run shares this id, which is what lets the calibration
    report see that forty rows off one chain are not forty independent observations.
    """
    cursor = conn.execute(
        """
        INSERT INTO scan_run (ran_at, session_date, symbols, config_digest, candidates, note)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (
            datetime.now(UTC).isoformat(),
            session_date.isoformat(),
            ",".join(sorted(symbols)),
            config_digest,
            note,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def record_opportunities(
    conn: sqlite3.Connection,
    scan_id: int,
    session_date: date,
    opportunities: list[Opportunity],
) -> int:
    """Write every candidate. Returns how many rows were added."""
    now = datetime.now(UTC).isoformat()
    added = 0

    for item in opportunities:
        short = _short_leg(item)
        conn.execute(
            """
            INSERT INTO opportunity_log (
                scan_id, recorded_at, session_date, symbol, strategy, expiry, dte,
                underlying_price, legs, short_strike, short_right, long_strike, width,
                credit, max_profit, max_loss, capital, commission,
                probability_of_profit, probability_of_touch, short_delta,
                iv, iv_rank, iv_confidence, liquidity_score, has_earnings, score,
                component_premium, component_iv_rank, component_liquidity,
                component_probability, component_event_risk
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                now,
                session_date.isoformat(),
                item.symbol,
                str(item.strategy),
                item.expiry.isoformat(),
                item.dte,
                item.underlying_price,
                json.dumps(
                    [
                        {
                            "action": str(leg.action),
                            "right": str(leg.right),
                            "strike": leg.strike,
                            "quantity": leg.quantity,
                            "mid": leg.mid,
                        }
                        for leg in item.legs
                    ],
                    separators=(",", ":"),
                ),
                short.strike if short else None,
                str(short.right) if short else None,
                None,
                _width(item),
                item.credit,
                item.max_profit,
                item.max_loss,
                item.capital,
                item.commission,
                item.probability_of_profit,
                item.probability_of_touch,
                item.short_delta,
                item.iv,
                item.iv_rank,
                item.iv_confidence,
                item.liquidity_score,
                int(item.has_earnings),
                item.score,
                item.components.premium,
                item.components.iv_rank,
                item.components.liquidity,
                item.components.probability,
                item.components.event_risk,
            ),
        )
        added += 1

    conn.execute("UPDATE scan_run SET candidates = candidates + ? WHERE id = ?", (added, scan_id))
    conn.commit()
    return added


def _row_to_logged(row: sqlite3.Row) -> LoggedOpportunity:
    keys = row.keys()
    return LoggedOpportunity(
        id=row["id"],
        scan_id=row["scan_id"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        session_date=date.fromisoformat(row["session_date"]),
        symbol=row["symbol"],
        strategy=row["strategy"],
        expiry=date.fromisoformat(row["expiry"]),
        dte=row["dte"],
        underlying_price=row["underlying_price"],
        short_strike=row["short_strike"],
        short_right=row["short_right"],
        width=row["width"],
        credit=row["credit"],
        max_profit=row["max_profit"],
        max_loss=row["max_loss"],
        probability_of_profit=row["probability_of_profit"],
        short_delta=row["short_delta"],
        iv=row["iv"],
        iv_rank=row["iv_rank"],
        liquidity_score=row["liquidity_score"],
        score=row["score"],
        outcome=row["outcome"] if "outcome" in keys else None,
        settlement_price=row["settlement_price"] if "settlement_price" in keys else None,
        finished_beyond=(
            bool(row["finished_beyond"])
            if "finished_beyond" in keys and row["finished_beyond"] is not None
            else None
        ),
        profit=row["profit"] if "profit" in keys else None,
        profit_fraction=row["profit_fraction"] if "profit_fraction" in keys else None,
    )


_JOINED = """
    SELECT o.*, r.outcome, r.settlement_price, r.finished_beyond, r.profit, r.profit_fraction
    FROM opportunity_log o
    LEFT JOIN opportunity_outcome r ON r.opportunity_id = o.id
"""


def unresolved(conn: sqlite3.Connection, asof: date) -> list[LoggedOpportunity]:
    """Logged candidates whose expiry has passed but which have no outcome yet."""
    rows = conn.execute(
        _JOINED + " WHERE r.opportunity_id IS NULL AND o.expiry <= ? ORDER BY o.expiry",
        (asof.isoformat(),),
    ).fetchall()
    return [_row_to_logged(row) for row in rows]


def resolved(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    strategy: str | None = None,
) -> list[LoggedOpportunity]:
    """Every candidate that has been settled."""
    query = _JOINED + " WHERE r.opportunity_id IS NOT NULL"
    params: list[object] = []
    if symbol:
        query += " AND o.symbol = ?"
        params.append(symbol.strip().upper())
    if strategy:
        query += " AND o.strategy = ?"
        params.append(strategy)
    query += " ORDER BY o.expiry, o.id"
    return [_row_to_logged(row) for row in conn.execute(query, params).fetchall()]


def all_logged(conn: sqlite3.Connection) -> list[LoggedOpportunity]:
    rows = conn.execute(_JOINED + " ORDER BY o.expiry, o.id").fetchall()
    return [_row_to_logged(row) for row in rows]


def record_outcome(
    conn: sqlite3.Connection,
    opportunity_id: int,
    *,
    settlement_date: date,
    settlement_price: float,
    settlement_source: str,
    outcome: str,
    finished_beyond: bool,
    profit: float,
    profit_fraction: float | None,
    note: str | None = None,
) -> bool:
    """Settle one candidate. Returns False if it was already settled.

    Refusing to overwrite matters: re-resolving with a different price source would
    silently rewrite history, and the whole point of this table is to be the record
    that was not adjusted after the fact.
    """
    try:
        conn.execute(
            """
            INSERT INTO opportunity_outcome (
                opportunity_id, resolved_at, settlement_date, settlement_price,
                settlement_source, outcome, finished_beyond, profit, profit_fraction, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_id,
                datetime.now(UTC).isoformat(),
                settlement_date.isoformat(),
                settlement_price,
                settlement_source,
                outcome,
                int(finished_beyond),
                profit,
                profit_fraction,
                note,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    return True


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Logged, resolved, and pending. What the report leads with."""
    logged = conn.execute("SELECT COUNT(*) FROM opportunity_log").fetchone()[0]
    settled = conn.execute("SELECT COUNT(*) FROM opportunity_outcome").fetchone()[0]
    scans = conn.execute("SELECT COUNT(*) FROM scan_run").fetchone()[0]
    return {
        "scans": int(scans),
        "logged": int(logged),
        "resolved": int(settled),
        "pending": int(logged) - int(settled),
    }
