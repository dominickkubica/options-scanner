"""Parquet snapshot storage.

Layout: data/snapshots/symbol=SPY/session_date=2026-07-30/20260730T194500Z.parquet

Partition values are also written as real columns inside each file. That is mild
duplication and it buys a lot: a single file is self describing when read on its own,
and a whole tree can be globbed without depending on hive partition inference.

One row per contract, with the snapshot level fields repeated on every row. Chains are
read as a table far more often than as an object, and a wide flat table is what both
duckdb and pandas want.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from optscan.logging import get_logger
from optscan.models import ChainSnapshot

log = get_logger("optscan.storage.snapshots")

SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("session_date", pa.date32()),
        ("captured_at", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("expiry", pa.date32()),
        ("right", pa.string()),
        ("strike", pa.float64()),
        ("contract_symbol", pa.string()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("last", pa.float64()),
        ("last_trade_at", pa.timestamp("us", tz="UTC")),
        ("volume", pa.int64()),
        ("open_interest", pa.int64()),
        ("vendor_iv", pa.float64()),
        ("in_the_money", pa.bool_()),
        ("contract_size", pa.int32()),
        ("currency", pa.string()),
        ("underlying_price", pa.float64()),
        ("quote_last", pa.float64()),
        ("quote_bid", pa.float64()),
        ("quote_ask", pa.float64()),
        ("chain_fetched_at", pa.timestamp("us", tz="UTC")),
        ("partial", pa.bool_()),
    ]
)


def snapshot_to_rows(snapshot: ChainSnapshot) -> list[dict[str, object]]:
    """Flatten a snapshot to one dict per contract, in schema column order."""
    rows: list[dict[str, object]] = []
    for chain in snapshot.chains:
        for contract in chain.contracts:
            rows.append(
                {
                    "symbol": snapshot.symbol,
                    "session_date": snapshot.session_date,
                    "captured_at": snapshot.fetched_at,
                    "source": snapshot.source,
                    "expiry": contract.expiry,
                    "right": str(contract.right),
                    "strike": contract.strike,
                    "contract_symbol": contract.contract_symbol,
                    "bid": contract.bid,
                    "ask": contract.ask,
                    "last": contract.last,
                    "last_trade_at": contract.last_trade_at,
                    "volume": contract.volume,
                    "open_interest": contract.open_interest,
                    "vendor_iv": contract.vendor_iv,
                    "in_the_money": contract.in_the_money,
                    "contract_size": contract.contract_size,
                    "currency": contract.currency,
                    "underlying_price": chain.underlying_price,
                    "quote_last": snapshot.quote.last,
                    "quote_bid": snapshot.quote.bid,
                    "quote_ask": snapshot.quote.ask,
                    "chain_fetched_at": chain.fetched_at,
                    "partial": snapshot.partial,
                }
            )
    return rows


def snapshot_to_table(snapshot: ChainSnapshot) -> pa.Table:
    """Arrow table with the fixed schema, so column types never drift between runs."""
    rows = snapshot_to_rows(snapshot)
    columns = {
        field.name: pa.array([row[field.name] for row in rows], type=field.type) for field in SCHEMA
    }
    return pa.table(columns, schema=SCHEMA)


def snapshot_dir(root: Path, symbol: str, session_date: date) -> Path:
    return root / f"symbol={symbol.upper()}" / f"session_date={session_date.isoformat()}"


def snapshot_filename(captured_at: datetime) -> str:
    """Capture time compacted to a sortable, filesystem safe name."""
    return captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + ".parquet"


def unique_path(directory: Path, captured_at: datetime) -> Path:
    """A path that does not exist yet, suffixing on collision.

    Filenames have one second resolution, so two captures inside the same second
    would otherwise overwrite each other. Losing a capture silently is worse than
    an ugly filename.
    """
    path = directory / snapshot_filename(captured_at)
    if not path.exists():
        return path
    stem = path.stem
    for counter in range(1, 1000):
        candidate = directory / f"{stem}-{counter}.parquet"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free filename in {directory} for {stem}")


def write_snapshot(snapshot: ChainSnapshot, root: Path) -> Path:
    """Write one snapshot and return the file path.

    Refuses to write an empty snapshot: a zero row parquet file in the history is
    indistinguishable from a real capture of a symbol that lost its listings.
    """
    if snapshot.contract_count == 0:
        raise ValueError(f"refusing to write an empty snapshot for {snapshot.symbol}")

    directory = snapshot_dir(root, snapshot.symbol, snapshot.session_date)
    directory.mkdir(parents=True, exist_ok=True)
    path = unique_path(directory, snapshot.fetched_at)

    table = snapshot_to_table(snapshot)
    pq.write_table(table, path, compression="zstd")

    log.info(
        "wrote snapshot",
        symbol=snapshot.symbol,
        session_date=snapshot.session_date.isoformat(),
        expiries=len(snapshot.chains),
        contracts=snapshot.contract_count,
        partial=snapshot.partial,
        path=str(path),
        bytes=path.stat().st_size,
    )
    return path


def snapshot_files(root: Path, symbol: str | None = None) -> list[Path]:
    """Every snapshot file under root, optionally for one symbol, sorted by path."""
    if not root.exists():
        return []
    pattern = f"symbol={symbol.upper()}/**/*.parquet" if symbol else "**/*.parquet"
    return sorted(root.glob(pattern))


def read_snapshots(
    root: Path,
    symbol: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load snapshots into a DataFrame via duckdb.

    Date columns come back as pandas datetime64, not python date. That is the pandas
    native form and what Phase 2 will want for vectorized work, but it means comparing
    a column against a date object silently matches nothing. Use .dt.date, or compare
    against a Timestamp.

    Returns an empty frame with the right columns when nothing matches, so callers
    never have to special case the first run of a fresh install.
    """
    files = snapshot_files(root, symbol)
    if not files:
        return pa.table({field.name: pa.array([], type=field.type) for field in SCHEMA}).to_pandas()

    conn = duckdb.connect()
    try:
        # duckdb renders timestamptz in the session timezone, which defaults to the
        # machine's. The instant is unchanged either way, but a capture time that reads
        # as 12:45 on one laptop and 15:45 on another is a trap in a tool whose whole
        # premise is sampling at a consistent time of day.
        conn.execute("SET TimeZone = 'UTC'")
        # The file list goes in as a parameter. read_parquet is a table function and
        # cannot take a subquery, and string interpolating paths into SQL is how you
        # get bitten by a symbol with a quote in it.
        query = "SELECT * FROM read_parquet(?) WHERE 1 = 1"
        params: list[object] = [[str(path) for path in files]]
        if start is not None:
            query += " AND session_date >= ?"
            params.append(start)
        if end is not None:
            query += " AND session_date <= ?"
            params.append(end)
        # "right" is quoted because SQL reads a bare RIGHT as the start of a join.
        query += ' ORDER BY session_date, expiry, strike, "right"'
        return conn.execute(query, params).df()
    finally:
        conn.close()
