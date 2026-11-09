"""Copies of a journal, made before a migration, so an older version can be restored.

This module imports nothing from the journal's database module: the database calls it, and a
backup failure reaches the caller as a JournalError there.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

BACKUP_FOLDER: Final = "backups"
BACKUP_GLOB: Final = "journal-schema*.db"


class JournalBackupError(Exception):
    """A journal cannot be copied; the message says why."""


def copy_journal(journal: Path, folder: Path) -> Path:
    """Copy a journal into folder, named journal-schema<version>.db, and return the copy."""
    try:
        source = sqlite3.connect(f"{journal.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        message = f"cannot open {journal}: {error}"
        raise JournalBackupError(message) from error
    try:
        (version,) = source.execute("PRAGMA user_version").fetchone()
        copy = folder / f"journal-schema{version}.db"
        copy.unlink(missing_ok=True)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    except (sqlite3.Error, OSError) as error:
        message = f"cannot back up {journal}: {error}"
        raise JournalBackupError(message) from error
    finally:
        source.close()
    return copy


def back_up_before_migration(journal: Path, keep: int) -> Path:
    """Copy the journal into its backups folder, keep the newest `keep` copies, return the copy."""
    folder = journal.parent / BACKUP_FOLDER
    try:
        folder.mkdir(exist_ok=True)
    except OSError as error:
        message = f"cannot back up {journal}: {error}"
        raise JournalBackupError(message) from error
    copy = copy_journal(journal.resolve(), folder)
    for stale in sorted(folder.glob(BACKUP_GLOB), key=_modified, reverse=True)[keep:]:
        stale.unlink(missing_ok=True)
    return copy


def newest_backup(journal: Path) -> Path | None:
    """Return the most recent backup of a journal, or None when it has none."""
    found = list((journal.parent / BACKUP_FOLDER).glob(BACKUP_GLOB))
    return max(found, key=_modified, default=None)


def _modified(path: Path) -> float:
    return path.stat().st_mtime
