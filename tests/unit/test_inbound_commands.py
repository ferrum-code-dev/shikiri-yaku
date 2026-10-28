from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.status import Status
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalOutbox,
    JournalSources,
    MessageStore,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.ports import EngineStatus

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.ports import ShikiruClient, ShikiruTask

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
COMMANDS = ("/status", "/open", "/help")


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Client:
    """A Shikiru client whose engine is up and which has no tasks."""

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=True, quotas={})

    async def list_tasks(self, _statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        return []


class Harness:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.messages = MessageStore(self.database, _Reporter())
        self.outbox = JournalOutbox(self.database, self.messages)
        self.clients = {"api": cast("ShikiruClient", _Client())}

    def send(self, text: str | None, update: int = 700) -> None:
        sources = JournalSources(CursorStore(self.database, self.signals), self.signals)
        reshower = ProductForwarder("product", self.clients, self.outbox, _Clock(), [OWNER])
        opened = OpenCommand(
            JournalOpen(self.database, self.signals),
            self.clients,
            reshower,
            self.outbox,
            _Clock(),
            destinations=[OWNER],
            channel="telegram",
        )
        commands = Commands(
            self.outbox, _Clock(), Status(self.clients, sources), opened, channel="telegram"
        )
        replies = Replies(
            JournalReplies(self.database, self.signals, OpStore(self.database)),
            self.outbox,
            _Clock(),
            self.clients,
            channel="telegram",
            commands=commands,
        )
        item = Inbound(update, InboundKind.TEXT, 1001, "1001/90", text=text)
        asyncio.run(replies.handle(item))


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


def test_status_is_answered_once_in_the_senders_chat_with_no_signal(h: Harness) -> None:
    h.send("/status")

    [pending] = h.outbox.pending()
    assert pending.destination == OWNER
    [stored] = h.messages.unconfirmed()
    assert (stored.signal_number, stored.purpose) == (None, Purpose.NOTICE)


@pytest.mark.parametrize("text", ["hello", "/start", None])
def test_text_that_is_not_a_command_gets_the_list_of_commands(h: Harness, text: str | None) -> None:
    h.send(text)

    [pending] = h.outbox.pending()
    assert all(command in pending.message.text for command in COMMANDS)


def test_the_same_update_delivered_twice_queues_one_reply(h: Harness) -> None:
    h.send("/status", update=700)
    h.send("/status", update=700)

    assert len(h.outbox.pending()) == 1
