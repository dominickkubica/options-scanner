"""Reading and writing held positions.

Thin on purpose. The interesting decisions are in the model and in the analytics; this
file exists so that neither of them has to know SQL.

One rule worth stating: a position and its legs are written in a single transaction. A
position row without its legs is not a partially saved position, it is a position whose
profit and loss is silently wrong, and the failure would surface days later as a number
nobody could explain.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from optscan.models.position import Position, PositionLeg, PositionStatus


def _row_to_leg(row: sqlite3.Row) -> PositionLeg:
    return PositionLeg(
        action=row["action"],
        right=row["right"],
        strike=row["strike"],
        expiry=date.fromisoformat(row["expiry"]),
        quantity=row["quantity"],
        fill_price=row["fill_price"],
        contract_size=row["contract_size"],
        contract_symbol=row["contract_symbol"],
    )


def _row_to_position(row: sqlite3.Row, legs: list[PositionLeg]) -> Position:
    return Position(
        id=row["id"],
        symbol=row["symbol"],
        strategy=row["strategy"],
        legs=tuple(legs),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        commission_open=row["commission_open"],
        commission_close=row["commission_close"],
        status=PositionStatus(row["status"]),
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        close_value=row["close_value"],
        note=row["note"],
    )


def add_position(conn: sqlite3.Connection, position: Position) -> Position:
    """Store a position and its legs, returning it with its assigned id."""
    cursor = conn.execute(
        """
        INSERT INTO position
            (symbol, strategy, opened_at, commission_open, commission_close,
             status, closed_at, close_value, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position.symbol,
            str(position.strategy) if position.strategy else None,
            position.opened_at.astimezone(UTC).isoformat(),
            position.commission_open,
            position.commission_close,
            str(position.status),
            position.closed_at.astimezone(UTC).isoformat() if position.closed_at else None,
            position.close_value,
            position.note,
        ),
    )
    identifier = int(cursor.lastrowid or 0)

    for leg in position.legs:
        conn.execute(
            """
            INSERT INTO position_leg
                (position_id, action, right, strike, expiry, quantity,
                 fill_price, contract_size, contract_symbol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                str(leg.action),
                str(leg.right),
                leg.strike,
                leg.expiry.isoformat(),
                leg.quantity,
                leg.fill_price,
                leg.contract_size,
                leg.contract_symbol,
            ),
        )

    conn.commit()
    return position.with_id(identifier)


def get_position(conn: sqlite3.Connection, identifier: int) -> Position | None:
    row = conn.execute("SELECT * FROM position WHERE id = ?", (identifier,)).fetchone()
    if row is None:
        return None
    legs = conn.execute(
        "SELECT * FROM position_leg WHERE position_id = ? ORDER BY id", (identifier,)
    ).fetchall()
    return _row_to_position(row, [_row_to_leg(leg) for leg in legs])


def list_positions(
    conn: sqlite3.Connection,
    *,
    status: PositionStatus | None = PositionStatus.OPEN,
    symbol: str | None = None,
) -> list[Position]:
    """Positions, newest first.

    Defaults to open only, because that is what every caller wants and a portfolio
    total that quietly included last year's closed trades would be nonsense. Pass
    status=None for everything.
    """
    query = "SELECT * FROM position WHERE 1 = 1"
    params: list[object] = []
    if status is not None:
        query += " AND status = ?"
        params.append(str(status))
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.strip().upper())
    query += " ORDER BY opened_at DESC, id DESC"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return []

    # One query for every leg rather than one per position. A portfolio is small, but
    # the N+1 would be the first thing to hurt once it is not.
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    leg_rows = conn.execute(
        f"SELECT * FROM position_leg WHERE position_id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()

    by_position: dict[int, list[PositionLeg]] = {identifier: [] for identifier in ids}
    for leg_row in leg_rows:
        by_position[leg_row["position_id"]].append(_row_to_leg(leg_row))

    return [_row_to_position(row, by_position[row["id"]]) for row in rows if by_position[row["id"]]]


def close_position(
    conn: sqlite3.Connection,
    identifier: int,
    close_value: float,
    *,
    commission: float = 0.0,
    when: datetime | None = None,
) -> Position | None:
    """Mark a position closed at a stated value. Returns the updated position."""
    position = get_position(conn, identifier)
    if position is None:
        return None
    if not position.is_open:
        raise ValueError(f"position {identifier} is already closed")

    closed = position.close(close_value, commission=commission, when=when)
    conn.execute(
        """
        UPDATE position
        SET status = ?, closed_at = ?, close_value = ?, commission_close = ?
        WHERE id = ?
        """,
        (
            str(closed.status),
            closed.closed_at.astimezone(UTC).isoformat(),  # type: ignore[union-attr]
            closed.close_value,
            closed.commission_close,
            identifier,
        ),
    )
    conn.commit()
    return closed


def delete_position(conn: sqlite3.Connection, identifier: int) -> int:
    """Remove a position and its legs. Returns 1 if it existed.

    For fixing a mistyped entry, not for closing a trade. Closing is a fact about the
    market and is recorded; deleting is an admission that the row should never have
    existed, and it takes the alert history with it.
    """
    cursor = conn.execute("DELETE FROM position WHERE id = ?", (identifier,))
    conn.commit()
    return cursor.rowcount


# --------------------------------------------------------------------------------
# Alert bookkeeping
# --------------------------------------------------------------------------------


def already_alerted(conn: sqlite3.Connection, position_id: int, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alert_sent WHERE position_id = ? AND kind = ? LIMIT 1",
        (position_id, kind),
    ).fetchone()
    return row is not None


def record_alert(
    conn: sqlite3.Connection,
    position_id: int,
    kind: str,
    detail: str | None = None,
) -> bool:
    """Note that an alert fired. Returns False if it had already fired.

    The uniqueness lives in the index rather than in a read then write, so two pollers
    racing cannot both decide they are the first.
    """
    try:
        conn.execute(
            "INSERT INTO alert_sent (position_id, kind, fired_at, detail) VALUES (?, ?, ?, ?)",
            (position_id, kind, datetime.now(UTC).isoformat(), detail),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    return True


def clear_alerts(conn: sqlite3.Connection, position_id: int) -> int:
    """Forget a position's fired alerts, so they can fire again.

    Wanted after a roll: the position is materially different, and suppressing its
    alerts because the old one already fired would hide the new one's first breach.
    """
    cursor = conn.execute("DELETE FROM alert_sent WHERE position_id = ?", (position_id,))
    conn.commit()
    return cursor.rowcount
