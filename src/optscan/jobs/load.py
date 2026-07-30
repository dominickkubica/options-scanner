"""Loading stored snapshots back into models.

The scan pipeline is pure and takes ChainSnapshot objects. Storage holds flat parquet
rows. This is the seam between them, and it lives in jobs/ because it does I/O.

Rebuilding a snapshot from rows loses nothing that matters: every field the models
carry is a column, provenance included.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from optscan.logging import get_logger
from optscan.models import ChainSnapshot, OptionChain, OptionContract, Quote
from optscan.storage import read_snapshots, snapshot_files

log = get_logger("optscan.jobs.load")


def latest_snapshot(root: Path, symbol: str) -> ChainSnapshot | None:
    """The most recent stored capture for one symbol, or None if there is none."""
    files = snapshot_files(root, symbol)
    if not files:
        return None

    frame = read_snapshots(root, symbol=symbol)
    if frame.empty:
        return None

    latest = frame["captured_at"].max()
    return snapshot_from_frame(frame[frame["captured_at"] == latest])


def snapshot_from_frame(frame: pd.DataFrame) -> ChainSnapshot | None:
    """Rebuild one ChainSnapshot from rows that share a capture time.

    Rows that fail validation are dropped and counted rather than failing the whole
    reconstruction. A single bad row in a 5000 row capture should cost that row.
    """
    if frame.empty:
        return None

    first = frame.iloc[0]
    symbol = str(first["symbol"])
    captured_at = pd.Timestamp(first["captured_at"]).to_pydatetime()
    source = str(first["source"])
    session_date = _as_date(first["session_date"])

    quote = Quote(
        symbol=symbol,
        last=_optional_float(first.get("quote_last")),
        bid=_optional_float(first.get("quote_bid")),
        ask=_optional_float(first.get("quote_ask")),
        fetched_at=captured_at,
        source=source,
    )

    chains: list[OptionChain] = []
    skipped = 0
    for expiry_value, rows in frame.groupby("expiry", sort=True):
        expiry = _as_date(expiry_value)
        contracts: list[OptionContract] = []
        for row in rows.itertuples():
            try:
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        contract_symbol=_optional_str(row.contract_symbol),
                        expiry=expiry,
                        strike=float(row.strike),
                        right=row.right,
                        bid=_optional_float(row.bid),
                        ask=_optional_float(row.ask),
                        last=_optional_float(row.last),
                        last_trade_at=_optional_datetime(row.last_trade_at),
                        volume=_optional_int(row.volume),
                        open_interest=_optional_int(row.open_interest),
                        vendor_iv=_optional_float(row.vendor_iv),
                        in_the_money=_optional_bool(row.in_the_money),
                        contract_size=int(row.contract_size),
                        currency=str(row.currency),
                        fetched_at=_optional_datetime(row.chain_fetched_at) or captured_at,
                        source=source,
                    )
                )
            except ValueError:
                skipped += 1

        if not contracts:
            continue

        chains.append(
            OptionChain(
                symbol=symbol,
                expiry=expiry,
                underlying_price=_optional_float(rows["underlying_price"].iloc[0]),
                contracts=tuple(contracts),
                fetched_at=contracts[0].fetched_at,
                source=source,
            )
        )

    if skipped:
        log.warning("dropped unparseable stored rows", symbol=symbol, skipped=skipped)
    if not chains:
        return None

    return ChainSnapshot(
        symbol=symbol,
        session_date=session_date,
        quote=quote,
        chains=tuple(chains),
        partial=bool(first.get("partial", False)),
        fetched_at=captured_at,
        source=source,
    )


def _as_date(value) -> date:
    return pd.Timestamp(value).date()


def _optional_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _optional_int(value) -> int | None:
    number = _optional_float(value)
    return None if number is None else int(number)


def _optional_bool(value) -> bool | None:
    if value is None or pd.isna(value):
        return None
    return bool(value)


def _optional_str(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _optional_datetime(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()
