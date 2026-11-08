"""Copies of a journal, made with SQLite's online backup, so an upgrade that fails can be undone."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from shikiri_yaku.journal.database import JournalError

if TYPE_CHECKING:
    from pathlib import Path


class JournalBackupError(JournalError):
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
    except sqlite3.Error as error:
        message = f"cannot back up {journal}: {error}"
        raise JournalBackupError(message) from error
    finally:
        source.close()
    return copy
