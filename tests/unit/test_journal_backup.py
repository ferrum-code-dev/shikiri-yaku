"""A journal is copied before it is migrated, so an older version can be restored."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalError,
    JournalTooNewError,
    SignalStore,
)
from shikiri_yaku.journal import database as journal_database
from shikiri_yaku.journal.migrations import MIGRATIONS
from shikiri_yaku.settings import JOURNAL_KEEP_BACKUPS

if TYPE_CHECKING:
    from pathlib import Path

PROJECT: Final = "api"
NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
BACKUPS: Final = "backups"
# keep: the schema a journal is at before the migration, so the older copies can have other numbers
OLD: Final = JOURNAL_KEEP_BACKUPS + 1


def _at_schema(monkeypatch: pytest.MonkeyPatch, number: int) -> None:
    """Make the journal module know only the first `number` migrations, as an older release did."""
    monkeypatch.setattr(journal_database, "MIGRATIONS", MIGRATIONS[:number])
    monkeypatch.setattr(journal_database, "LATEST_VERSION", number)


def _old_journal(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _at_schema(monkeypatch, OLD)
    journal = Database.open(path)
    CursorStore(journal, SignalStore(journal)).record_batch(PROJECT, [], 10, None, NOW)
    journal.close()
    monkeypatch.undo()


def _open_at_old(path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """Open a journal without migrating it: it must already be at the old schema."""
    _at_schema(monkeypatch, OLD)
    try:
        return Database.open(path)
    finally:
        monkeypatch.undo()


def _version(journal: Database) -> int:
    version: int = journal.connection.execute("PRAGMA user_version").fetchone()[0]
    return version


def _last_event_id(journal: Database) -> int:
    row = journal.connection.execute(
        "SELECT last_event_id FROM cursors WHERE project = ?", (PROJECT,)
    ).fetchone()
    return int(row["last_event_id"])


def test_a_migration_copies_the_old_journal_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    _old_journal(path, monkeypatch)

    Database.open(path).close()

    copy = _open_at_old(tmp_path / BACKUPS / f"journal-schema{OLD}.db", monkeypatch)
    try:
        assert _version(copy) == OLD
        assert _last_event_id(copy) == 10
    finally:
        copy.close()


def test_the_oldest_backup_is_removed_when_a_newer_one_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    _old_journal(path, monkeypatch)
    folder = tmp_path / BACKUPS
    folder.mkdir()
    older = [folder / f"journal-schema{number}.db" for number in range(1, OLD)]
    for stamp, backup in enumerate(older, start=1000):
        backup.write_bytes(b"old")
        os.utime(backup, (stamp, stamp))

    Database.open(path).close()

    remaining = sorted(backup.name for backup in folder.glob("journal-schema*.db"))
    assert remaining == sorted([older[1].name, older[2].name, f"journal-schema{OLD}.db"])


def test_a_fresh_or_current_journal_is_not_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"

    Database.open(path).close()
    Database.open(path).close()

    assert not (tmp_path / BACKUPS).exists()


def test_a_failed_backup_stops_the_migration_and_leaves_the_journal_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    _old_journal(path, monkeypatch)
    folder = tmp_path / BACKUPS
    folder.mkdir()
    folder.chmod(0o500)
    try:
        with pytest.raises(JournalError, match="cannot back up"):
            Database.open(path)
        journal = _open_at_old(path, monkeypatch)
        try:
            version = _version(journal)
        finally:
            journal.close()
    finally:
        folder.chmod(0o700)

    assert version == OLD


def test_a_too_new_journal_names_the_newest_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    _old_journal(path, monkeypatch)
    Database.open(path).close()
    journal = Database.open(path)
    journal.connection.execute(f"PRAGMA user_version = {journal_database.LATEST_VERSION + 1}")
    journal.close()

    with pytest.raises(JournalTooNewError) as caught:
        Database.open(path)

    copy = tmp_path / BACKUPS / f"journal-schema{OLD}.db"
    assert str(copy) in str(caught.value)
    assert "copy the backup" in str(caught.value)
