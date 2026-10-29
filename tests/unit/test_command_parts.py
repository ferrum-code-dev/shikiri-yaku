"""A command reply longer than one Telegram message arrives in parts, and each part is queued once.

Each test runs the real journal, outbox and sender, and the real TelegramChannel over the fake Bot
API, on an in-memory journal. Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Self, cast

import pytest

from shikiri_yaku.adapters.telegram import TelegramChannel
from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.status import Status
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import refusal_notice, split_reply
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalOutbox,
    JournalSources,
    MessageStore,
    SignalStore,
)
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, EngineStatus, ShikiruLinkDownError
from tests.support.secrets import SECRETS
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.ports import EscalationView, ShikiruClient, ShikiruTask

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
COMPONENTS: Final = 60
TOKEN: Final = SECRETS["telegram_bot_token"]


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Client:
    """A Shikiru client whose engine is up, with no tasks and no escalations."""

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=True, quotas={})

    async def list_tasks(self, _statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        return []

    async def list_escalations(self) -> list[EscalationView]:
        return []


class _SilentClient(_Client):
    """A Shikiru client that does not answer, so its project is unreachable."""

    async def list_escalations(self) -> list[EscalationView]:
        message = "link is down"
        raise ShikiruLinkDownError(message)


def _names() -> list[str]:
    """Names that pass the name pattern and are long enough that their lists need parts."""
    return [f"component-{index:02d}-" + "x" * 60 for index in range(COMPONENTS)]


class _Stack:
    """The journal, outbox and sender, with the real Telegram Channel over the fake Bot API."""

    def __init__(self, client: type[_Client]) -> None:
        clients = {name: cast("ShikiruClient", client()) for name in _names()}
        self.database = Database.open(":memory:")
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, _Reporter()))
        self.bot = FakeBotApi()
        channel = TelegramChannel(self.bot, _Clock(), self.outbox)
        self.sender = OutboxSender(self.outbox, channel, _Clock(), interval_s=1)
        signals = SignalStore(self.database)
        self.status = Status(clients, JournalSources(CursorStore(self.database, signals), signals))
        reshower = ProductForwarder("product", clients, self.outbox, _Clock(), [OWNER])
        opened = OpenCommand(
            JournalOpen(self.database, signals),
            clients,
            reshower,
            self.outbox,
            _Clock(),
            destinations=[OWNER],
            channel="telegram",
        )
        self.commands = Commands(self.outbox, _Clock(), self.status, opened, channel="telegram")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.database.close()

    def command(self, text: str, update: int = 700) -> None:
        asyncio.run(
            self.commands.handle(Inbound(update, InboundKind.TEXT, 1001, "1001/90", text=text))
        )

    def deliver(self) -> None:
        asyncio.run(self.sender.tick())

    def sent(self) -> list[str]:
        """The text of each message the Bot API was asked to send, in order."""
        return [
            str(payload["text"]) for method, payload in self.bot.calls if method == "sendMessage"
        ]


def test_an_over_limit_status_is_sent_in_parts_that_together_hold_its_whole_text() -> None:
    with _Stack(_Client) as stack:
        stack.command("/status")
        stack.deliver()
        report = asyncio.run(stack.status.report())
        sent = stack.sent()

    assert len(report) > MESSAGE_TEXT_LIMIT
    assert len(sent) > 1
    assert all(len(part) <= MESSAGE_TEXT_LIMIT for part in sent)
    assert " ".join(sent).split() == report.split()


def test_an_over_limit_unreachable_notice_is_sent_in_parts_that_name_every_project() -> None:
    with _Stack(_SilentClient) as stack:
        stack.command("/open")
        stack.deliver()
        sent = stack.sent()

    joined = " ".join(sent)
    assert len(sent) > 1
    assert all(len(part) <= MESSAGE_TEXT_LIMIT for part in sent)
    assert all(name in joined for name in _names())


@pytest.mark.parametrize(("text", "client"), [("/status", _Client), ("/open", _SilentClient)])
def test_replaying_an_update_queues_no_part_again(text: str, client: type[_Client]) -> None:
    with _Stack(client) as stack:
        stack.command(text)
        stack.deliver()
        first = stack.sent()
        stack.command(text)
        queued_again = stack.outbox.pending()
        stack.deliver()
        sent_again = stack.sent()

    assert len(first) > 1
    assert queued_again == []
    assert sent_again == first


def test_a_secret_that_a_cut_would_split_is_refused_whole_and_no_part_of_it_is_sent() -> None:
    # keep: the token starts before the first cut and ends after it, so no part holds all of it
    reply = "b" * 4060 + TOKEN + "c" * 100
    with _Stack(_Client) as stack:
        queue_command_reply(stack.outbox, [OWNER], reply, key="command:700", now=NOW)
        stack.deliver()
        sent = stack.sent()

    assert sent == [refusal_notice(None)]


@pytest.mark.parametrize(
    ("text", "limit", "parts"),
    [
        ("abc def ghi", 10, ["abc def", "ghi"]),
        ("ab cd\nef gh", 6, ["ab cd", "ef gh"]),
        ("ab\ncd\n\nef gh", 8, ["ab\ncd", "ef gh"]),
        ("abcdefghijklmno", 10, ["abcdefghij", "klmno"]),
    ],
)
def test_a_reply_is_cut_at_the_best_break_that_fits_and_nothing_else_is_dropped(
    text: str, limit: int, parts: list[str]
) -> None:
    assert split_reply(text, limit) == parts
