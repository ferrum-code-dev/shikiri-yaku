"""The composition root and the console entry point: the only module that builds concrete objects.

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written. `main` gives the command line this module's composition,
so the command line never imports this module and the concrete objects stay here (design §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.shikiru_mcp import probe_link, stdio_connector
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database, JournalError
from shikiri_yaku.ports import CompositionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import Connector
    from shikiri_yaku.ports import Clock
    from shikiri_yaku.settings import Settings


SHIKIRU_EXECUTABLE: Final = "shikiru"

type LinkFactory = Callable[[str, Path], Connector]


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    clock: Clock
    journal: Database
    link_probes: Mapping[str, Callable[[], Awaitable[None]]]

    def close(self) -> None:
        self.journal.close()


def compose(
    settings: Settings, directory: Path, *, link: LinkFactory = stdio_connector
) -> Composition:
    """Build the objects of one instance; the caller closes the result.

    `link` opens a project's MCP link; tests give it a fake.
    """
    try:
        journal = Database.open(directory / JOURNAL_FILE_NAME)
    except JournalError as error:
        raise CompositionError(str(error)) from error
    timeout_s = settings.operator.link_call_timeout_s
    probes = {
        component.name: partial(probe_link, link(SHIKIRU_EXECUTABLE, component.path), timeout_s)
        for component in settings.components
    }
    return Composition(settings, SystemClock(), journal, probes)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(argv, compose)
