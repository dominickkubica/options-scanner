"""Persistence: sqlite for config and positions, parquet/duckdb for snapshots."""

from optscan.storage.db import (
    add_symbol,
    connect,
    has_run,
    list_watchlist,
    migrate,
    recent_runs,
    record_run,
    remove_symbol,
    seed_watchlist,
    session,
)
from optscan.storage.snapshots import (
    read_snapshots,
    snapshot_files,
    snapshot_to_table,
    write_snapshot,
)

__all__ = [
    "add_symbol",
    "connect",
    "has_run",
    "list_watchlist",
    "migrate",
    "read_snapshots",
    "recent_runs",
    "record_run",
    "remove_symbol",
    "seed_watchlist",
    "session",
    "snapshot_files",
    "snapshot_to_table",
    "write_snapshot",
]
