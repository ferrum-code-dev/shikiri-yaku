"""Copies of each product's journal that an update makes, so a failed upgrade can be undone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal import JOURNAL_FILE_NAME, JournalError, copy_journal
from shikiri_yaku.settings import product_names
from shikiri_yaku.update.host import UpdateError
from shikiri_yaku.update.store import prune

if TYPE_CHECKING:
    from pathlib import Path

BACKUP_DIR_NAME: Final = "backups"
BACKUP_GLOB: Final = "journal-schema*.db"


@dataclass(frozen=True)
class Backup:
    product: str
    journal: Path
    copy: Path


def backup_journals(home: Path, keep: int) -> None:
    """Copy every product's journal into its backups folder and keep the newest `keep` copies."""
    for product in product_names(home):
        journal = home / product / JOURNAL_FILE_NAME
        if not journal.is_file():
            continue
        folder = journal.parent / BACKUP_DIR_NAME
        folder.mkdir(exist_ok=True)
        try:
            copy_journal(journal, folder)
        except JournalError as error:
            raise UpdateError(str(error)) from error
        prune(list(folder.glob(BACKUP_GLOB)), keep)


def existing_backups(home: Path) -> list[Backup]:
    found: list[Backup] = []
    for product in product_names(home):
        journal = home / product / JOURNAL_FILE_NAME
        folder = journal.parent / BACKUP_DIR_NAME
        found.extend(Backup(product, journal, copy) for copy in sorted(folder.glob(BACKUP_GLOB)))
    return found


def restore_hint(backup: Backup) -> str:
    return (
        f"{backup.product}: if the old version stops with JournalTooNewError, stop its service, "
        f"copy {backup.copy} over {backup.journal}, delete {backup.journal}-wal and "
        f"{backup.journal}-shm if they exist, and start the service again. Changes made after "
        "the backup are lost."
    )
