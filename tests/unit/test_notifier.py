from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.journal import Database, JournalOutbox, MessageStore
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from collections.abc import Generator

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
WHERE = Destination("telegram", "100", 7)


class FakeClock:
    def now(self) -> datetime:
        return START

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class FakeReporter:
    def report(self, line: str) -> None:
        del line


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def test_a_notice_is_queued_once_per_key_without_a_signal_number(memory: Database) -> None:
    outbox = JournalOutbox(memory, MessageStore(memory, FakeReporter()))
    notifier = Notifier(outbox, FixedDestinations([WHERE]), FakeClock())
    signal = Signal("api", Kind.ESCALATION, "E1", "agent_error", "blocked", "T-1", "E1")

    notifier.tell(signal, "Retrying E1 once.", "retry-notice:E1")
    notifier.tell(signal, "Retrying E1 once.", "retry-notice:E1")

    pending = outbox.pending()
    assert [(item.destination, item.message.text) for item in pending] == [
        (WHERE, "Retrying E1 once.")
    ]
    numbers = memory.connection.execute("SELECT signal_number FROM messages").fetchall()
    assert [row[0] for row in numbers] == [None]
