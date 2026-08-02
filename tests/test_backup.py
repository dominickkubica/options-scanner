"""Backup, mirror, rotation.

The assertions that matter are the ones about what is *not* lost: the mirror never
deletes, rotation never eats the newest copy, and a database copy that fails its
integrity check is reported as a failure rather than left sitting there looking like
a backup.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.backup import (
    DB_DIR,
    SNAPSHOT_DIR,
    backup_database,
    mirror_snapshots,
    rotate,
    run_backup,
)


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS thing (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO thing (name) VALUES (?)", [(f"row {n}",) for n in range(rows)]
        )
        conn.commit()


def _count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM thing").fetchone()[0]


def _make_capture(root: Path, symbol: str, session: str, content: bytes = b"parquet") -> Path:
    path = root / f"symbol={symbol}" / f"session_date={session}" / "capture.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
    )


class TestBackupDatabase:
    def test_the_copy_holds_the_same_rows(self, tmp_path: Path) -> None:
        source = tmp_path / "source.sqlite"
        _make_db(source, rows=5)

        size, _ = backup_database(source, tmp_path / "copy.sqlite")

        assert size > 0
        assert _count(tmp_path / "copy.sqlite") == 5

    def test_the_copy_is_not_left_open(self, tmp_path: Path) -> None:
        """On Windows an open handle locks the file, and rotation could not delete it."""
        source = tmp_path / "source.sqlite"
        _make_db(source)
        destination = tmp_path / "copy.sqlite"

        backup_database(source, destination)

        destination.unlink()
        assert not destination.exists()

    def test_creates_the_destination_directory(self, tmp_path: Path) -> None:
        source = tmp_path / "source.sqlite"
        _make_db(source)
        destination = tmp_path / "deep" / "deeper" / "copy.sqlite"

        backup_database(source, destination)

        assert destination.exists()

    def test_copies_a_database_that_is_open_for_writing(self, tmp_path: Path) -> None:
        """A file copy can tear here. The sqlite backup API is why this uses it."""
        source = tmp_path / "source.sqlite"
        _make_db(source, rows=2)

        with closing(sqlite3.connect(source)) as writer:
            writer.execute("INSERT INTO thing (name) VALUES ('during')")
            writer.commit()
            backup_database(source, tmp_path / "copy.sqlite")

        assert _count(tmp_path / "copy.sqlite") == 3


class TestMirrorSnapshots:
    def test_copies_every_capture_the_first_time(self, tmp_path: Path) -> None:
        source = tmp_path / "snapshots"
        _make_capture(source, "SPY", "2026-07-30")
        _make_capture(source, "SPY", "2026-07-31")
        _make_capture(source, "QQQ", "2026-07-30")

        result = mirror_snapshots(source, tmp_path / "mirror")

        assert result.copied == 3
        assert result.present == 0
        assert (
            tmp_path / "mirror" / "symbol=SPY" / "session_date=2026-07-30" / "capture.parquet"
        ).exists()

    def test_second_run_copies_nothing(self, tmp_path: Path) -> None:
        """Captures are immutable, so a daily mirror costs a day, not the history."""
        source = tmp_path / "snapshots"
        _make_capture(source, "SPY", "2026-07-30")
        mirror_snapshots(source, tmp_path / "mirror")

        result = mirror_snapshots(source, tmp_path / "mirror")

        assert result.copied == 0
        assert result.present == 1

    def test_only_the_new_day_is_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "snapshots"
        _make_capture(source, "SPY", "2026-07-30")
        mirror_snapshots(source, tmp_path / "mirror")
        _make_capture(source, "SPY", "2026-07-31")

        result = mirror_snapshots(source, tmp_path / "mirror")

        assert result.copied == 1
        assert result.present == 1

    def test_a_capture_deleted_from_the_source_stays_in_the_mirror(self, tmp_path: Path) -> None:
        """The mirror is not a sync. Deleting from it would delete the history."""
        source = tmp_path / "snapshots"
        path = _make_capture(source, "SPY", "2026-07-30")
        mirror_snapshots(source, tmp_path / "mirror")
        path.unlink()

        mirror_snapshots(source, tmp_path / "mirror")

        survivor = (
            tmp_path / "mirror" / "symbol=SPY" / "session_date=2026-07-30" / "capture.parquet"
        )
        assert survivor.exists()

    def test_a_changed_size_is_recopied(self, tmp_path: Path) -> None:
        source = tmp_path / "snapshots"
        _make_capture(source, "SPY", "2026-07-30", b"short")
        mirror_snapshots(source, tmp_path / "mirror")
        _make_capture(source, "SPY", "2026-07-30", b"a much longer capture")

        result = mirror_snapshots(source, tmp_path / "mirror")

        assert result.copied == 1

    def test_missing_source_is_a_note_not_a_crash(self, tmp_path: Path) -> None:
        result = mirror_snapshots(tmp_path / "nothing", tmp_path / "mirror")

        assert result.copied == 0
        assert any("nothing was mirrored" in note for note in result.notes)


class TestRotate:
    def test_keeps_the_newest(self, tmp_path: Path) -> None:
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            (tmp_path / f"optscan-{stamp}.sqlite").write_bytes(b"x")

        removed = rotate(tmp_path, keep=2)

        assert [path.name for path in removed] == ["optscan-20260101T000000Z.sqlite"]
        assert len(list(tmp_path.glob("optscan-*.sqlite"))) == 2

    def test_under_the_limit_removes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "optscan-20260101T000000Z.sqlite").write_bytes(b"x")
        assert rotate(tmp_path, keep=30) == []

    def test_missing_directory_removes_nothing(self, tmp_path: Path) -> None:
        assert rotate(tmp_path / "nothing", keep=5) == []

    def test_ignores_files_it_did_not_write(self, tmp_path: Path) -> None:
        (tmp_path / "optscan-20260101T000000Z.sqlite").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("keep me")

        rotate(tmp_path, keep=0)

        assert (tmp_path / "notes.txt").exists()


class TestRunBackup:
    def test_refuses_when_there_is_no_database(self, settings: Settings) -> None:
        result = run_backup(settings)

        assert result.ok is False
        assert any("nothing to back up" in note for note in result.notes)

    def test_copies_the_database_and_mirrors_the_captures(self, settings: Settings) -> None:
        _make_db(settings.sqlite_path, rows=4)
        _make_capture(settings.snapshot_path, "SPY", "2026-07-30")

        result = run_backup(settings)

        assert result.ok is True
        assert result.database is not None
        assert result.database.exists()
        assert result.database.parent == settings.backup_path / DB_DIR
        assert result.files_copied == 1
        assert (settings.backup_path / SNAPSHOT_DIR).exists()

    def test_each_run_writes_a_new_dated_copy(self, settings: Settings) -> None:
        _make_db(settings.sqlite_path)

        first = run_backup(settings, now=datetime(2026, 8, 1, 20, 30, tzinfo=UTC))
        second = run_backup(settings, now=datetime(2026, 8, 2, 20, 30, tzinfo=UTC))

        assert first.database != second.database
        assert len(list((settings.backup_path / DB_DIR).glob("*.sqlite"))) == 2

    def test_rotation_applies_to_the_database_copies(self, tmp_path: Path, clean_env: None) -> None:
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            backup_dir=tmp_path / "backups",
            backup_keep=2,
        )
        _make_db(settings.sqlite_path)

        for day in (1, 2, 3, 4):
            run_backup(settings, now=datetime(2026, 8, day, 20, 30, tzinfo=UTC))

        assert len(list((settings.backup_path / DB_DIR).glob("*.sqlite"))) == 2

    def test_says_so_when_the_backup_shares_a_drive(self, settings: Settings) -> None:
        """A same drive copy survives a mistake, not the drive. It should not imply more."""
        _make_db(settings.sqlite_path)

        result = run_backup(settings)

        assert any("same drive" in note for note in result.notes)

    def test_reports_a_failure_rather_than_raising(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from optscan.jobs import backup

        _make_db(settings.sqlite_path)

        def explode(source: Path, destination: Path) -> tuple[int, list[str]]:
            raise OSError("the backup drive is not there")

        monkeypatch.setattr(backup, "backup_database", explode)

        result = run_backup(settings)

        assert result.ok is False
        assert any("not there" in note for note in result.notes)
