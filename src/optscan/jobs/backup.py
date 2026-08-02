"""Backing up the two things in `data/` that cannot be rebuilt.

Everything this tool computes can be recomputed. Two things cannot be refetched at any
price:

- **The parquet chain snapshots.** No free vendor sells an option chain as it stood at
  15:45 last Tuesday. This is the IV history.
- **The sqlite database.** It holds the validation study: every candidate the screen
  surfaced, the score it was given at the time, and the outcomes settled against it. A
  candidate that was never logged when it was scored cannot be logged later.

So the two are backed up differently, because they fail differently.

## The database is copied through sqlite, not through the filesystem

A file copy of a live sqlite database can be torn: the copy can catch a write in
progress, or catch the file without the write ahead log that completes it, and the
result is a file that opens fine and is missing rows. `Connection.backup` takes a
consistent copy of a database that is being written to, which is the whole reason it
exists.

Then the copy is opened and integrity checked. An unverified backup is not a backup, it
is a file that will turn out to be unreadable on the one day it matters.

## The snapshots are mirrored incrementally, and never rotated

Parquet captures are immutable once written and partitioned by symbol and session date,
so a mirror only ever copies files it does not already have. That keeps a daily backup
at roughly the size of a daily capture rather than the size of the history.

Nothing is ever deleted from the mirror. Rotation on the database means keeping the last
N dated copies, which is a real safety property: a corruption noticed a week late is
still recoverable. Rotation on the mirror would mean deleting captures, which is the
exact loss the mirror exists to prevent.

## A backup on the same drive is half a backup

It protects against the mistakes, which are common, and not against the drive, which is
what people picture when they say backup. `optscan backup` says which one you have
rather than letting the word imply the stronger claim.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from optscan.config import Settings
from optscan.logging import get_logger

log = get_logger("optscan.jobs.backup")

#: Directory inside the backup root holding the dated database copies.
DB_DIR = "db"
#: Directory inside the backup root mirroring the parquet captures.
SNAPSHOT_DIR = "snapshots"
#: Timestamp format for a dated copy. UTC, and sortable, so rotation is a sort.
STAMP = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class BackupResult:
    ok: bool
    database: Path | None = None
    database_bytes: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    files_present: int = 0
    rotated: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class _Mirror:
    copied: int = 0
    copied_bytes: int = 0
    present: int = 0
    notes: list[str] = field(default_factory=list)


def backup_database(source: Path, destination: Path) -> tuple[int, list[str]]:
    """Copy a sqlite database consistently, then verify the copy opens and passes.

    Returns the size of the copy and any notes. Raises if the copy cannot be verified,
    because a backup that silently failed verification is worse than no backup: it is a
    backup someone is relying on.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    # closing() rather than the connection's own context manager, which commits the
    # transaction and leaves the connection open. On Windows an open handle keeps the
    # file locked, and rotation then fails to delete the copies it wrote itself.
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(destination)) as copy:
        origin.backup(copy)

    with closing(sqlite3.connect(destination)) as check:
        row = check.execute("PRAGMA integrity_check").fetchone()
    verdict = row[0] if row else "no result"
    if verdict != "ok":
        raise RuntimeError(f"the database copy failed its integrity check: {verdict}")

    return destination.stat().st_size, notes


def mirror_snapshots(source: Path, destination: Path) -> _Mirror:
    """Copy every capture the mirror does not already have.

    Existence is decided on the relative path plus the byte count. A capture is written
    once and never edited, so a file of the same size at the same path is the same file,
    and comparing contents would read the whole history every day to learn nothing.
    """
    result = _Mirror()
    if not source.exists():
        result.notes.append(f"No snapshot directory at {source}, so nothing was mirrored.")
        return result

    for path in sorted(source.rglob("*.parquet")):
        relative = path.relative_to(source)
        target = destination / relative
        size = path.stat().st_size
        if target.exists() and target.stat().st_size == size:
            result.present += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        result.copied += 1
        result.copied_bytes += size

    return result


def rotate(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` dated database copies.

    Sorted by name, which is why the stamp is UTC and zero padded: sorting by mtime
    would reorder the history the first time a file was touched by a sync client.
    """
    if not directory.exists():
        return []
    copies = sorted(directory.glob("optscan-*.sqlite"))
    doomed = copies[:-keep] if keep < len(copies) else []

    removed: list[Path] = []
    for path in doomed:
        # A copy held open by a sync client is a housekeeping problem, not a reason to
        # fail a backup that has already been written and verified. It goes next time.
        try:
            path.unlink()
        except OSError as error:
            log.warning("could not rotate an old copy", path=str(path), error=str(error))
            continue
        removed.append(path)
    return removed


def run_backup(settings: Settings, *, now: datetime | None = None) -> BackupResult:
    """Back up the database and mirror the captures. Safe to run repeatedly."""
    moment = now or datetime.now(UTC)
    root = settings.backup_path
    notes: list[str] = []

    if not settings.sqlite_path.exists():
        return BackupResult(
            ok=False,
            notes=(
                f"No database at {settings.sqlite_path}, so there is nothing to back up. "
                "Run a job that writes one first.",
            ),
        )

    stamp = moment.strftime(STAMP)
    destination = root / DB_DIR / f"optscan-{stamp}.sqlite"

    try:
        size, db_notes = backup_database(settings.sqlite_path, destination)
    except (sqlite3.Error, OSError, RuntimeError) as error:
        log.error("database backup failed", error=str(error))
        return BackupResult(ok=False, notes=(f"{type(error).__name__}: {error}",))
    notes.extend(db_notes)

    mirror = mirror_snapshots(settings.snapshot_path, root / SNAPSHOT_DIR)
    notes.extend(mirror.notes)

    rotated = rotate(root / DB_DIR, settings.backup_keep)

    if settings.backup_path_is_same_volume:
        notes.append(
            f"This backup is on the same drive as the data it copies ({root.anchor}). "
            "That survives a mistake but not the drive. Set OPTSCAN_BACKUP_DIR to "
            "another disk or a synced folder to get a copy that survives losing this one."
        )

    log.info(
        "backup complete",
        database=str(destination),
        database_bytes=size,
        mirrored=mirror.copied,
        already_present=mirror.present,
        rotated=len(rotated),
    )

    return BackupResult(
        ok=True,
        database=destination,
        database_bytes=size,
        files_copied=mirror.copied,
        bytes_copied=mirror.copied_bytes,
        files_present=mirror.present,
        rotated=tuple(rotated),
        notes=tuple(notes),
    )
