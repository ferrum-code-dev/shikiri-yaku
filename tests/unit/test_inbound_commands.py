from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.status import Status
from shikiri_yaku.bootstrap import goal_command
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
from shikiri_yaku.ports import Ack, AckStatus, EngineStatus, ShikiruLinkDownError

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.ports import ShikiruClient, ShikiruTask

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
COMMANDS = ("/status", "/open", "/goal", "/help")
DONE = Ack(id=7, status=AckStatus.DONE, result={"task_id": "T-9"}, message=None)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Client:
    """A Shikiru client whose engine is up, which has no tasks, and which answers each submit."""

    def __init__(self) -> None:
        self.answer: Ack | Exception = DONE
        self.requests: list[str] = []

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=True, quotas={})

    async def list_tasks(self, _statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        return []

    async def submit_task(self, request: str) -> Ack:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Harness:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.messages = MessageStore(self.database, _Reporter())
        self.outbox = JournalOutbox(self.database, self.messages)
        self.client = _Client()
        self.clients = {"api": cast("ShikiruClient", self.client)}

    def replies(self) -> Replies:
        sources = JournalSources(CursorStore(self.database, self.signals), self.signals)
        reshower = ProductForwarder(
            "product", self.clients, self.outbox, _Clock(), lambda _project, _kind: [OWNER]
        )
        opened = OpenCommand(
            JournalOpen(self.database, self.signals),
            self.clients,
            reshower,
            self.outbox,
            _Clock(),
            destinations=lambda _project, _kind: [OWNER],
            channel="telegram",
        )
        goal = goal_command(self.database, self.clients, self.outbox, _Clock())
        commands = Commands(
            self.outbox, _Clock(), Status(self.clients, sources), opened, goal, channel="telegram"
        )
        return Replies(
            JournalReplies(self.database, self.signals, OpStore(self.database)),
            self.outbox,
            _Clock(),
            self.clients,
            channel="telegram",
            commands=commands,
            goal=goal,
        )

    def send(self, text: str | None, update: int = 700) -> None:
        item = Inbound(update, InboundKind.TEXT, 1001, "1001/90", text=text)
        asyncio.run(self.replies().handle(item))

    def tap(self, data: str, ref: str, update: int = 800) -> None:
        asyncio.run(self.replies().handle(Inbound(update, InboundKind.TAP, 1001, ref, data=data)))


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


def test_submit_again_in_the_chat_retries_the_goal_it_asks_about(h: Harness) -> None:
    h.client.answer = ShikiruLinkDownError("link down")
    h.send("/goal api Add the export")
    [question] = h.outbox.pending()
    [button] = question.message.buttons
    h.outbox.confirm(question.message_id, "1001/44")
    h.client.answer = DONE
    h.tap(button.data, "1001/44")
    [reply] = h.outbox.pending()
    assert "T-9" in reply.message.text
    assert h.client.requests == ["Add the export"] * 2
