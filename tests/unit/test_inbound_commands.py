from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

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
from shikiri_yaku.settings import (
    BUTTON_LABEL_MAX_CHARS,
    COMMAND_NAME_MAX_CHARS,
    MESSAGE_TITLE_MAX_CHARS,
)
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.ports import EscalationView, ShikiruClient, ShikiruTask

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
COMMANDS = ("/status", "/open", "/goal", "/help")
NOTHING = "Nothing is waiting for you."
DONE = Ack(id=7, status=AckStatus.DONE, result={"task_id": "T-9"}, message=None)
API_TOPIC: Final = 77
WEB_TOPIC: Final = 78
UNNAMED_TOPIC: Final = 79
TOPICS: Final = {API_TOPIC: "api", WEB_TOPIC: "web"}


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

    async def list_escalations(self) -> list[EscalationView]:
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
        commands = Commands(
            self.outbox,
            _Clock(),
            Status(self.clients, sources),
            opened,
            goal,
            destinations=FixedDestinations([OWNER], topics=TOPICS),
            channel="telegram",
            name_max_chars=COMMAND_NAME_MAX_CHARS,
        )
        return Replies(
            JournalReplies(
                self.database,
                self.signals,
                OpStore(self.database),
                channel_name="telegram",
            ),
            self.outbox,
            _Clock(),
            self.clients,
            channel="telegram",
            commands=commands,
            goal=goal,
        )

    def send(self, text: str | None, update: int = 700, thread: int | None = None) -> None:
        item = Inbound(update, InboundKind.TEXT, 1001, 1001, "1001/90", text=text, thread=thread)
        asyncio.run(self.replies().handle(item))

    def tap(self, data: str, ref: str, update: int = 800) -> None:
        asyncio.run(
            self.replies().handle(Inbound(update, InboundKind.TAP, 1001, 1001, ref, data=data))
        )


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


@pytest.mark.parametrize(
    ("thread", "sections"),
    [
        (None, ["api", "web"]),
        (UNNAMED_TOPIC, ["api", "web"]),
        (API_TOPIC, ["api"]),
        (WEB_TOPIC, ["web"]),
    ],
)
def test_status_reports_the_component_of_its_topic_and_every_component_elsewhere(
    h: Harness, thread: int | None, sections: list[str]
) -> None:
    h.clients["web"] = cast("ShikiruClient", _Client())

    h.send("/status", thread=thread)

    [pending] = h.outbox.pending()
    assert [part.split("\n")[0] for part in pending.message.text.split("\n\n")] == sections


def test_a_repeated_status_in_a_component_topic_queues_one_reply(h: Harness) -> None:
    h.send("/status", update=700, thread=API_TOPIC)
    h.send("/status", update=700, thread=API_TOPIC)

    assert len(h.outbox.pending()) == 1


@pytest.mark.parametrize("text", ["hello", "/help", None])
def test_free_text_and_help_get_the_list_of_commands(h: Harness, text: str | None) -> None:
    h.send(text)

    [pending] = h.outbox.pending()
    assert all(command in pending.message.text for command in COMMANDS)


@pytest.mark.parametrize(
    ("text", "first_line"),
    [
        ("/stats", "Unknown command /stats."),
        ("/start", "Unknown command /start."),
        ("/Open", NOTHING),
        ("/open@bot", NOTHING),
    ],
)
def test_a_slash_message_is_answered_by_the_command_it_names(
    h: Harness, text: str, first_line: str
) -> None:
    h.send(text)

    [pending] = h.outbox.pending()
    assert pending.message.text.split("\n")[0] == first_line


def test_an_unknown_command_is_followed_by_the_list_of_commands(h: Harness) -> None:
    h.send("/stats")

    [pending] = h.outbox.pending()
    assert all(command in pending.message.text for command in COMMANDS)


def test_a_long_unknown_name_is_cut_to_the_limit(h: Harness) -> None:
    text = "/" + "x" * COMMAND_NAME_MAX_CHARS * 3
    h.send(text)

    [pending] = h.outbox.pending()
    assert (
        text in pending.message.text,
        "x" * (COMMAND_NAME_MAX_CHARS + 1) in pending.message.text,
        pending.message.text.startswith("Unknown command /x"),
    ) == (False, False, True)


def test_open_with_nothing_open_is_answered_once_and_a_repeat_of_the_update_is_not(
    h: Harness,
) -> None:
    h.send("/open", update=700)
    h.send("/open", update=700)

    [pending] = h.outbox.pending()
    assert pending.message.text == NOTHING


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
