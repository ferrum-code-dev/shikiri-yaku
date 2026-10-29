"""A real Notifier over a journal outbox, so a test reads what the PO would be told."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.journal import JournalOutbox, MessageStore
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from shikiri_yaku.journal import Database
    from shikiri_yaku.ports import Clock

WHERE = Destination("telegram", "100", 7)


class FailingReporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


def journal_outbox(database: Database) -> JournalOutbox:
    return JournalOutbox(database, MessageStore(database, FailingReporter()))


def journal_notifier(outbox: JournalOutbox, clock: Clock) -> Notifier:
    return Notifier(outbox, FixedDestinations([WHERE]), clock)
