"""The composition root and the console entry point: the only module that builds concrete objects.

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written. `main` gives the command line this module's composition,
so the command line never imports this module and the concrete objects stay here (design §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database, JournalError
from shikiri_yaku.ports import CompositionError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shikiri_yaku.ports import Clock
    from shikiri_yaku.settings import Settings


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


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(argv, compose)
