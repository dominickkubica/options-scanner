"""A local cache of intraday option trades, for the short premium study.

Layout: data/option_tape/underlying=QQQ/session=2025-04-07/ holding `underlying.parquet`,
`options.parquet` and `manifest.json`.

## What this is and is not

A cache of what Alpaca's historical option bars returned for one session: one minute
trade bars for a strike ladder around the day's range, for the expiries that session
and the next, beside the underlying's raw minute bars. It is **not** a chain history.
There is no bid, no ask and no open interest, because the vendor has no historical
option quotes at all (see `providers/alpaca.py`). Nothing here feeds the IV history,
whose one writer is the 15:45 capture.

Kept on disk because a study reads the same sessions many times over and the whole
range is several thousand requests to fetch.

## Written whole or not at all

Each file goes to a temporary name and is renamed into place, and the manifest is
written last. A session counts as held only once its manifest exists, so an interrupted
sync resumes cleanly rather than treating half a day as done.

A session whose ladder returned no trades at all is still written, with an empty
options table. "Asked and nothing traded" is an answer; "never asked" is not, and the
manifest is what tells the two apart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from optscan.logging import get_logger
from optscan.models import PriceBar
from optscan.tickers import is_ticker, normalize_ticker

log = get_logger("optscan.storage.option_tape")

TAPE_DIR_NAME = "option_tape"
UNDERLYING_FILE = "underlying.parquet"
OPTIONS_FILE = "options.parquet"
MANIFEST_FILE = "manifest.json"

_TS = pa.timestamp("us", tz="UTC")

UNDERLYING_SCHEMA = pa.schema(
    [
        ("ts", _TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)

OPTIONS_SCHEMA = pa.schema(
    [
        ("contract", pa.string()),
        ("expiry", pa.date32()),
        ("right", pa.string()),
        ("strike", pa.float64()),
        ("ts", _TS),
        ("close", pa.float64()),
        ("vwap", pa.float64()),
        ("volume", pa.int64()),
        ("trades", pa.int64()),
    ]
)


@dataclass(frozen=True, slots=True)
class TapeSession:
    """One stored session, as read back."""

    underlying: str
    session: date
    minutes: pd.DataFrame
    options: pd.DataFrame
    manifest: dict


def tape_root(data_path: Path) -> Path:
    return data_path / TAPE_DIR_NAME


def session_dir(root: Path, underlying: str, session: date) -> Path:
    # A validated ticker is one path component, so this cannot land outside root.
    ticker = normalize_ticker(underlying)
    return root / f"underlying={ticker}" / f"session={session.isoformat()}"


def has_session(root: Path, underlying: str, session: date) -> bool:
    return (session_dir(root, underlying, session) / MANIFEST_FILE).is_file()


def sessions_held(root: Path, underlying: str) -> list[date]:
    """Every session stored for one underlying, oldest first."""
    if not is_ticker(underlying):
        return []
    base = root / f"underlying={normalize_ticker(underlying)}"
    if not base.is_dir():
        return []
    held = []
    for manifest in base.glob(f"session=*/{MANIFEST_FILE}"):
        try:
            held.append(date.fromisoformat(manifest.parent.name.removeprefix("session=")))
        except ValueError:
            continue
    return sorted(held)


def _minutes_table(minutes: Sequence[PriceBar]) -> pa.Table:
    return pa.table(
        {
            "ts": pa.array([bar.ts for bar in minutes], type=_TS),
            "open": pa.array([bar.open for bar in minutes], type=pa.float64()),
            "high": pa.array([bar.high for bar in minutes], type=pa.float64()),
            "low": pa.array([bar.low for bar in minutes], type=pa.float64()),
            "close": pa.array([bar.close for bar in minutes], type=pa.float64()),
            "volume": pa.array([int(bar.volume or 0) for bar in minutes], type=pa.int64()),
        },
        schema=UNDERLYING_SCHEMA,
    )


def _options_table(tape: Mapping[str, Sequence[Mapping[str, object]]]) -> pa.Table:
    rows = [(contract, row) for contract, series in sorted(tape.items()) for row in series]
    columns = {
        "contract": [contract for contract, _ in rows],
        **{name: [row[name] for _, row in rows] for name in OPTIONS_SCHEMA.names[1:]},
    }
    return pa.table(
        {field.name: pa.array(columns[field.name], type=field.type) for field in OPTIONS_SCHEMA},
        schema=OPTIONS_SCHEMA,
    )


def _replace(path: Path, write) -> None:
    temporary = path.with_name(path.name + ".tmp")
    write(temporary)
    temporary.replace(path)


def write_session(
    root: Path,
    underlying: str,
    session: date,
    minutes: Sequence[PriceBar],
    tape: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    manifest: Mapping[str, object],
) -> Path:
    """Store one session and return its directory.

    Refuses a session with no underlying minutes: without the underlying no option
    print can be turned into a volatility, so the options alone would be a file that
    looks like data and cannot be used.
    """
    if not minutes:
        raise ValueError(f"refusing to store {underlying} {session} with no underlying minutes")

    directory = session_dir(root, underlying, session)
    if root.resolve() not in directory.resolve().parents:
        raise ValueError(f"refusing to write option tape outside {root}: {directory}")
    directory.mkdir(parents=True, exist_ok=True)

    options = _options_table(tape)
    _replace(
        directory / UNDERLYING_FILE,
        lambda path: pq.write_table(_minutes_table(minutes), path, compression="zstd"),
    )
    _replace(
        directory / OPTIONS_FILE,
        lambda path: pq.write_table(options, path, compression="zstd"),
    )
    record = {
        **manifest,
        "underlying": normalize_ticker(underlying),
        "session": session.isoformat(),
        "minutes": len(minutes),
        "contracts_with_trades": sum(1 for series in tape.values() if series),
        "bars": options.num_rows,
    }
    _replace(
        directory / MANIFEST_FILE,
        lambda path: path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8"),
    )
    log.info(
        "stored option tape",
        underlying=record["underlying"],
        session=record["session"],
        contracts=record["contracts_with_trades"],
        bars=record["bars"],
    )
    return directory


def read_session(root: Path, underlying: str, session: date) -> TapeSession | None:
    """One stored session, or None when it has not been fetched."""
    if not is_ticker(underlying) or not has_session(root, underlying, session):
        return None
    directory = session_dir(root, underlying, session)
    return TapeSession(
        underlying=normalize_ticker(underlying),
        session=session,
        minutes=pq.read_table(directory / UNDERLYING_FILE).to_pandas(),
        options=pq.read_table(directory / OPTIONS_FILE).to_pandas(),
        manifest=json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8")),
    )
