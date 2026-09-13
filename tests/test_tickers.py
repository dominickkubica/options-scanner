"""A symbol is a ticker or it is refused (SEC-0001, SEC-0003).

A symbol becomes a path component under the snapshot root. Unchecked, the scan's
`symbols` query read parquet files from wherever `SPY/../..` pointed, and a symbol typed
into `optscan watchlist add` could make the capture job write outside the data folder.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.storage import db, snapshots
from optscan.tickers import is_ticker, normalize_ticker


@pytest.mark.parametrize("good", ["SPY", "brk.b", " bf-b ", "^VIX", "GOOGL"])
def test_real_tickers_pass(good: str) -> None:
    assert is_ticker(good)
    assert normalize_ticker(good) == good.strip().upper()


@pytest.mark.parametrize(
    "bad", ["", "   ", "SPY/../..", "A/B", "A\\B", "..", ".SPY", "SP Y", "X" * 17, None]
)
def test_anything_else_is_refused(bad: str | None) -> None:
    assert not is_ticker(bad)
    with pytest.raises(ValueError, match="not a ticker"):
        normalize_ticker(bad)


def test_a_traversal_symbol_finds_nothing_even_with_a_parquet_file_above_root(
    tmp_path: Path,
) -> None:
    """The reproduced attack: a real parquet two levels above the snapshot root."""
    root = tmp_path / "data" / "snapshots"
    inside = root / "symbol=SPY" / "session_date=2026-09-11"
    inside.mkdir(parents=True)
    (inside / "20260911T194500Z.parquet").write_bytes(b"PAR1")
    (tmp_path / "OUTSIDE.parquet").write_bytes(b"PAR1")

    assert snapshots.snapshot_files(root, "SPY/../../..") == []
    assert snapshots.snapshot_files(root, "SPY") == [inside / "20260911T194500Z.parquet"]


def test_no_snapshot_path_is_built_for_a_symbol_with_separators(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a ticker"):
        snapshots.snapshot_dir(tmp_path, "A/../../pwn", date(2026, 9, 11))


def test_the_watchlist_refuses_a_path(tmp_settings: Settings) -> None:
    with db.session(tmp_settings.sqlite_path) as conn:
        with pytest.raises(ValueError, match="not a ticker"):
            db.add_symbol(conn, "A/../../../tmp/pwn")
        assert db.list_watchlist(conn) == []


def test_the_scan_query_refuses_a_path_symbol_by_name(tmp_settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from optscan.api.app import create_app

    tmp_settings.ensure_dirs()
    with TestClient(create_app(tmp_settings)) as client:
        response = client.get("/api/scan", params={"symbols": "SPY/../.."})
    assert response.status_code == 422
    assert "SPY/../.." in response.json()["detail"]
