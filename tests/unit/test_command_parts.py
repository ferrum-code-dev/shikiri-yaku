"""A command reply longer than one Telegram message arrives in parts, and each part is queued once.

Each test runs the real journal, outbox and sender, and the real TelegramChannel over the fake Bot
API, on an in-memory journal. Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Self, cast, overload

import pytest

from shikiri_yaku.adapters.telegram import TelegramChannel
from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.status import Status
from shikiri_yaku.bootstrap import goal_command
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
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    EngineStatus,
    Message,
    ShikiruLinkDownError,
)
from shikiri_yaku.settings import (
    BUTTON_LABEL_MAX_CHARS,
    COMMAND_NAME_MAX_CHARS,
    MESSAGE_TITLE_MAX_CHARS,
)
from tests.support.destinations import FixedDestinations
from tests.support.secrets import SECRETS
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from shikiri_yaku.ports import EscalationView, ShikiruClient, ShikiruTask

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
COMPONENTS: Final = 60
TOKEN: Final = SECRETS["bot_token"]


class _InterruptedParts(Sequence[Message]):
    def __len__(self) -> int:
        return 2

    @overload
    def __getitem__(self, index: int) -> Message: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Message]: ...

    def __getitem__(self, index: int | slice) -> Message | Sequence[Message]:
        if isinstance(index, slice):
            return [Message("alpha"), Message("beta")][index]
        if index == 0:
            return Message("alpha")
        raise KeyboardInterrupt


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

    def __init__(self) -> None:
        self.engine_up = True
        self.status_calls = 0

    async def status(self) -> EngineStatus:
        self.status_calls += 1
        return EngineStatus(engine_up=self.engine_up, quotas={})

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
        self.raw_clients = {name: client() for name in _names()}
        self.clients = {
            name: cast("ShikiruClient", instance) for name, instance in self.raw_clients.items()
        }
        self.database = Database.open(":memory:")
        self.messages = MessageStore(self.database, _Reporter())
        self.outbox = JournalOutbox(self.database, self.messages)
        self.bot = FakeBotApi()
        signals = SignalStore(self.database)
        self.signals = signals
        self.sources = JournalSources(CursorStore(self.database, signals), signals)
        self.status: Status
        self.rebuild_commands()

    def rebuild_commands(self) -> None:
        self.outbox = JournalOutbox(self.database, self.messages)
        channel = TelegramChannel(self.bot, _Clock(), self.outbox)
        self.sender = OutboxSender(self.outbox, channel, _Clock(), interval_s=1)
        self.status = Status(self.clients, self.sources)
        reshower = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            self.clients,
            self.outbox,
            _Clock(),
            FixedDestinations([OWNER]),
            button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
        )
        opened = OpenCommand(
            JournalOpen(self.database, self.signals),
            self.clients,
            reshower,
            self.outbox,
            _Clock(),
            destinations=FixedDestinations([OWNER]),
            channel="telegram",
        )
        goal = goal_command(self.database, self.clients, self.outbox, _Clock(), FixedDestinations())
        self.commands = Commands(
            self.outbox,
            _Clock(),
            self.status,
            opened,
            goal,
            channel="telegram",
            name_max_chars=COMMAND_NAME_MAX_CHARS,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.database.close()

    def command(self, text: str, update: int = 700) -> None:
        asyncio.run(
            self.commands.handle(
                Inbound(update, InboundKind.TEXT, 1001, 1001, "1001/90", text=text)
            )
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


def test_replay_after_live_status_changes_reuses_the_committed_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _Stack(_Client) as stack:
        queue_group = stack.outbox.queue_command_reply

        def interrupt_after_commit(
            destinations: Sequence[Destination],
            parts: Sequence[Message],
            *,
            key: str,
            now: datetime,
        ) -> None:
            queue_group(destinations, parts, key=key, now=now)
            raise KeyboardInterrupt

        monkeypatch.setattr(stack.outbox, "queue_command_reply", interrupt_after_commit)
        with pytest.raises(KeyboardInterrupt):
            stack.command("/status")
        first_pending = stack.outbox.pending()
        first_parts = [pending.message.text for pending in first_pending]
        calls = [client.status_calls for client in stack.raw_clients.values()]

        for client in stack.raw_clients.values():
            client.engine_up = False
        stack.rebuild_commands()
        stack.command("/status")
        queued = stack.outbox.pending()
        stack.deliver()
        sent = stack.sent()

    assert len(first_parts) > 1
    assert [client.status_calls for client in stack.raw_clients.values()] == calls
    assert queued == first_pending
    assert sent == first_parts


def test_interruption_during_part_insertion_rolls_back_the_group() -> None:
    key = "command:701"
    with _Stack(_Client) as stack:
        with pytest.raises(KeyboardInterrupt):
            stack.outbox.queue_command_reply([OWNER], _InterruptedParts(), key=key, now=NOW)
        assert not stack.outbox.command_reply_queued(key)
        assert stack.outbox.pending() == []

        stack.outbox.queue_command_reply(
            [OWNER], [Message("alpha"), Message("beta")], key=key, now=NOW
        )
        stack.deliver()
        sent = stack.sent()

    assert sent == ["alpha", "beta"]


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
