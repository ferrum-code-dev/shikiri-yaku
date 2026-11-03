"""The SQLite connection of the journal: WAL, numbered migrations, and transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Self

from shikiri_yaku.journal.migrations import MIGRATIONS

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

BUSY_TIMEOUT_S: Final = 5
LATEST_VERSION: Final = len(MIGRATIONS)


class JournalError(Exception):
    """The journal file cannot be opened or used; the message says why."""


class JournalTooNewError(JournalError):
    """The database was written by a newer version of the code, so this one must not touch it."""


@dataclass(frozen=True)
class Inserted:
    """Result of an insert-if-absent: the row id, and whether this call created the row."""

    row_id: int
    is_new: bool


def stamp(moment: datetime) -> str:
    """Return the text stored for a time; always UTC, so text order is time order."""
    if moment.tzinfo is None:
        message = "journal times must carry a time zone"
        raise ValueError(message)
    return moment.astimezone(UTC).isoformat()


def parse_stamp(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


class Database:
    """One connection. A connection belongs to one thread; another thread opens its own."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._in_transaction = False

    @classmethod
    def open(cls, path: Path | str) -> Self:
        """Open or create the journal and bring its schema up to date.

        Whatever SQLite refuses is raised as JournalError, so callers never handle sqlite3 errors.
        """
        try:
            connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        except sqlite3.Error as error:
            message = f"cannot open {path}: {error}"
            raise JournalError(message) from error
        database = cls(connection)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            database._migrate()
        except sqlite3.Error as error:
            connection.close()
            message = f"cannot use {path}: {error}"
            raise JournalError(message) from error
        except BaseException:
            connection.close()
            raise
        return database

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Commit the block's changes together or not at all; a nested block joins the outer one."""
        if self._in_transaction:
            yield self._connection
            return
        self._connection.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
        finally:
            self._in_transaction = False

    def _migrate(self) -> None:
        with self.transaction() as connection:
            version: int = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > LATEST_VERSION:
                message = f"journal is at schema {version}, this code knows up to {LATEST_VERSION}"
                raise JournalTooNewError(message)
            for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {number}")
