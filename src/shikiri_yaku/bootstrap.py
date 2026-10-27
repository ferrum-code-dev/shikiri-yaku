"""The composition root: the only module that builds concrete objects (design §12).

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database, JournalError

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.ports import Clock
    from shikiri_yaku.settings import Settings


class CompositionError(Exception):
    """The instance cannot be wired, because its journal cannot be opened or used."""


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    clock: Clock
    journal: Database

    def close(self) -> None:
        self.journal.close()


def compose(settings: Settings, directory: Path) -> Composition:
    """Build the objects of one instance; the caller closes the result."""
    try:
        journal = Database.open(directory / JOURNAL_FILE_NAME)
    except JournalError as error:
        raise CompositionError(str(error)) from error
    return Composition(settings, SystemClock(), journal)
