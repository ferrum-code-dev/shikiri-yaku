"""Ending an Expert conversation: each way it ends leaves none of its rows, files or topic."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_runs import ConversationRuns, conversation_directory
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertResult, ExpertSession
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.ports import (
    TOPIC_GONE_REASON,
    ChannelError,
    ChannelRefusedError,
    Expert,
    Message,
    ShikiruTask,
)
from tests.support.expert_proposals import expert_proposals

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator
    from pathlib import Path

    from shikiri_yaku.domain.expert import ExpertRequest
    from shikiri_yaku.ports import Channel, EscalationView, ShikiruClient

OWNER: Final = 1001
CHAT: Final = -100123
GROUP_ID: Final = -100123
PRIVATE_CHAT: Final = OWNER
CHANNEL: Final = "telegram"
RETENTION: Final = timedelta(days=14)
NOW: Final = datetime(2026, 1, 1, tzinfo=UTC)
CREATED: Final = 500
TARGET_THREAD: Final = 22
BYSTANDER_THREAD: Final = 33


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Reporter:
    def report(self, line: str) -> None:
        raise AssertionError(line)


class Client:
    async def list_tasks(self, statuses: object = None) -> list[ShikiruTask]:
        del statuses
        return []

    async def list_escalations(self) -> list[EscalationView]:
        return []


class StalledClient(Client):
    """Stalls while the turn gathers its context, until the test cancels the turn."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def list_tasks(self, statuses: object = None) -> list[ShikiruTask]:
        del statuses
        self.started.set()
        await asyncio.Event().wait()
        return []


class Topics:
    """Deletes topics; each deletion raises the next scripted refusal first."""

    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.refusals: list[Exception] = []

    async def delete(self, thread_id: int) -> None:
        if self.refusals:
            raise self.refusals.pop(0)
        self.deleted.append(thread_id)

    async def create_expert(self, name: str) -> int:
        raise NotImplementedError

    async def rename(self, thread_id: int, name: str) -> None:
        raise NotImplementedError

    async def addressed(self, word: str) -> bool:
        raise NotImplementedError

    async def bot_name(self) -> str:
        return "bot"

    def link(self, thread_id: int) -> str:
        raise NotImplementedError


class AnsweringExpert:
    """Answers at once, or with `hold` waits until its call is cancelled."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.started = asyncio.Event()
        self.cancelled = False

    async def turn(self, request: ExpertRequest) -> ExpertResult:
        del request
        self.started.set()
        if self.hold:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return ExpertResult(
            answer=ExpertAnswer(reply="Ready", title="", tasks=[], findings=""),
            session=ExpertSession(rung=0, provider="test", session_id="s", findings=""),
            call=None,
        )


class RefusingChannel:
    """Refuses every send with the error it was built with."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def send(self, destination: Destination, message: Message) -> str:
        del destination, message
        raise self._error


class RefusingThreadChannel:
    def __init__(self, thread_id: int) -> None:
        self._thread_id = thread_id

    async def send(self, destination: Destination, message: Message) -> str:
        del message
        if destination.thread == self._thread_id:
            raise ChannelRefusedError(TOPIC_GONE_REASON)
        return f"{destination.chat}/{destination.thread}"


class Harness:
    def __init__(self, path: Path, expert: Expert, client: Client) -> None:
        self.database = Database.open(":memory:")
        self.log = Conversations(self.database)
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, Reporter()))
        self.clock = Clock()
        self.topics = Topics()
        self.directory = path
        self.runs = ConversationRuns()
        self.ending = ExpertEnding(
            self.log,
            self.runs,
            self.topics,
            self.outbox,
            self.clock,
            directory=path,
            channel=CHANNEL,
            group_id=GROUP_ID,
            retention=RETENTION,
        )
        clients = {"api": cast("ShikiruClient", client)}
        self.turns = ExpertTurns(
            self.log,
            self.outbox,
            self.clock,
            expert=expert,
            topics=self.topics,
            clients=clients,
            scope={"api": path / "api"},
            directory=path,
            channel=CHANNEL,
            group_id=GROUP_ID,
            runs=self.runs,
            ending=self.ending,
            proposals=expert_proposals(
                self.database,
                self.log,
                self.outbox,
                self.clock,
                clients,
                channel=CHANNEL,
                group_id=GROUP_ID,
            ),
        )

    def open_conversation(self, update_id: int, thread: int, *, origin_chat: int = CHAT) -> int:
        """Open a conversation as a creation does, with its directory and one file in it."""
        self.log.create(
            update_id,
            chat=origin_chat,
            thread=None,
            text="question",
            scope=["api"],
            now=self.clock.now(),
        )
        self.log.opened(update_id, thread)
        row = self.log.get(update_id)
        assert row is not None
        directory = conversation_directory(self.directory, row.id)
        directory.mkdir(parents=True)
        (directory / "notes.txt").write_text("notes", encoding="utf-8")
        return row.id

    def remaining(self, conversation_id: int) -> int:
        """Count the rows of a conversation that are left, across every table that holds one."""
        queries = (
            "SELECT COUNT(*) FROM turns WHERE conversation_id = ?",
            "SELECT COUNT(*) FROM topics WHERE conversation_id = ?",
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            "SELECT COUNT(*) FROM conversations WHERE id = ?",
        )
        total = 0
        for query in queries:
            row = self.database.connection.execute(query, (conversation_id,)).fetchone()
            total += int(row[0])
        return total

    def notices(self, key: str) -> int:
        row = self.database.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ?", (key,)
        ).fetchone()
        return int(row[0])


@pytest.fixture
def build(tmp_path: Path) -> Generator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(expert: AnsweringExpert | None = None, client: Client | None = None) -> Harness:
        harness = Harness(tmp_path, expert or AnsweringExpert(), client or Client())
        made.append(harness)
        return harness

    yield make
    for harness in made:
        harness.database.close()


def _inbound(kind: InboundKind, thread: int, text: str | None = None) -> Inbound:
    return Inbound(501, kind, OWNER, CHAT, "501", text=text, thread=thread)


async def _topic_closed(h: Harness, conversation: int, thread: int) -> None:
    del conversation
    await h.turns.handle(_inbound(InboundKind.TOPIC_CLOSED, thread))


async def _close_command(h: Harness, conversation: int, thread: int) -> None:
    del conversation
    await h.turns.handle(_inbound(InboundKind.TEXT, thread, "/close"))


async def _expired(h: Harness, conversation: int, thread: int) -> None:
    del conversation, thread
    h.clock.current = NOW + RETENTION + timedelta(days=1)
    await h.ending.sweep()


async def _topic_gone_on_send(h: Harness, conversation: int, thread: int) -> None:
    h.outbox.queue(
        None,
        Purpose.EXPERT,
        [Destination(CHANNEL, str(GROUP_ID), thread)],
        Message("the answer"),
        key="ask:answer:501",
        now=h.clock.now(),
        conversation_id=conversation,
    )
    sender = OutboxSender(
        h.outbox,
        cast("Channel", RefusingChannel(ChannelRefusedError(TOPIC_GONE_REASON))),
        h.clock,
        interval_s=1,
        gone=h.ending,
    )
    await sender.tick()


@pytest.mark.parametrize(
    "end",
    [_topic_closed, _close_command, _expired, _topic_gone_on_send],
    ids=["topic-closed", "close-command", "expired", "topic-gone-on-send"],
)
def test_each_ending_leaves_no_rows_files_or_topic_and_touches_no_other_conversation(
    build: Callable[..., Harness],
    end: Callable[[Harness, int, int], Awaitable[None]],
) -> None:
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    bystander = h.open_conversation(CREATED + 100, BYSTANDER_THREAD)
    h.log.touch(bystander, NOW + timedelta(days=3))

    asyncio.run(end(h, target, TARGET_THREAD))

    assert h.remaining(target) == 0
    assert not conversation_directory(h.directory, target).exists()
    assert h.remaining(bystander) > 0
    assert conversation_directory(h.directory, bystander).exists()
    assert h.topics.deleted == ([] if end is _topic_gone_on_send else [TARGET_THREAD])


def test_expiry_counts_from_the_last_message_by_the_clock(build: Callable[..., Harness]) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    h.clock.current = NOW + timedelta(days=10)
    asyncio.run(h.turns.handle(_inbound(InboundKind.TEXT, TARGET_THREAD, "/help")))

    h.clock.current = NOW + timedelta(days=20)
    asyncio.run(h.ending.sweep())
    assert h.remaining(conversation) > 0

    h.clock.current = NOW + timedelta(days=25)
    asyncio.run(h.ending.sweep())
    assert h.remaining(conversation) == 0


def test_a_refused_deletion_keeps_the_conversation_reports_once_and_is_retried(
    build: Callable[..., Harness],
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    h.topics.refusals = [ChannelRefusedError("cannot delete"), ChannelRefusedError("cannot delete")]
    report_key = f"expert:deletion:{conversation}"

    async def scenario() -> None:
        await h.ending.end(conversation)
        await h.ending.sweep()
        assert h.remaining(conversation) > 0
        assert h.notices(report_key) == 1
        await h.ending.sweep()

    asyncio.run(scenario())

    assert h.remaining(conversation) == 0
    assert not conversation_directory(h.directory, conversation).exists()
    assert h.topics.deleted == [TARGET_THREAD]


def test_a_deletion_that_fails_for_now_is_retried_without_a_report(
    build: Callable[..., Harness],
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    h.topics.refusals = [ChannelError("network down")]

    async def scenario() -> None:
        await h.ending.end(conversation)
        assert h.remaining(conversation) > 0
        await h.ending.sweep()

    asyncio.run(scenario())

    assert h.remaining(conversation) == 0
    assert h.notices(f"expert:deletion:{conversation}") == 0


def test_a_topic_that_is_already_gone_ends_its_conversation_without_a_report(
    build: Callable[..., Harness],
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    h.topics.refusals = [ChannelRefusedError(TOPIC_GONE_REASON)]

    asyncio.run(h.ending.end(conversation))

    assert h.remaining(conversation) == 0
    assert h.notices(f"expert:deletion:{conversation}") == 0
    assert h.topics.deleted == []


def test_closing_a_conversation_stops_its_running_turn_and_erases_it(
    build: Callable[..., Harness],
) -> None:
    expert = AnsweringExpert(hold=True)
    h = build(expert)
    conversation = h.open_conversation(CREATED, TARGET_THREAD)

    async def scenario() -> None:
        running = asyncio.create_task(h.turns.run(conversation, CREATED))
        await asyncio.wait_for(expert.started.wait(), timeout=1)
        await asyncio.wait_for(_topic_closed(h, conversation, TARGET_THREAD), timeout=1)
        await asyncio.wait_for(running, timeout=1)

    asyncio.run(scenario())

    assert expert.cancelled
    assert h.remaining(conversation) == 0
    assert not conversation_directory(h.directory, conversation).exists()
    assert h.topics.deleted == [TARGET_THREAD]


def test_an_ending_erases_the_recorded_messages_and_replies_of_its_topic_only(
    build: Callable[..., Harness],
) -> None:
    h = build()
    items = [
        Inbound(CREATED, InboundKind.TEXT, OWNER, PRIVATE_CHAT, str(CREATED), text="/ask question"),
        Inbound(501, InboundKind.TEXT, OWNER, CHAT, "501", text="/help", thread=TARGET_THREAD),
        Inbound(502, InboundKind.TEXT, OWNER, CHAT, "502", text="/help", thread=BYSTANDER_THREAD),
        Inbound(503, InboundKind.TEXT, OWNER, CHAT, "503", text="/close", thread=TARGET_THREAD),
        Inbound(504, InboundKind.TEXT, OWNER, CHAT, "504", text="component", thread=77),
    ]
    JournalInbound(h.database).record_batch(CHANNEL, items, next_offset=505, now=NOW)
    target = h.open_conversation(CREATED, TARGET_THREAD, origin_chat=PRIVATE_CHAT)
    bystander = h.open_conversation(CREATED + 100, BYSTANDER_THREAD)
    h.outbox.queue_command_reply(
        [Destination(CHANNEL, str(PRIVATE_CHAT), None)],
        [Message("Open the Expert conversation.")],
        key=f"ask:open:{CREATED}",
        now=NOW,
        conversation_id=target,
    )

    async def scenario() -> None:
        await h.turns.handle(items[1])
        await h.turns.handle(items[2])
        await h.turns.handle(items[3])

    asyncio.run(scenario())

    def count(query: str, params: tuple[object, ...]) -> int:
        row = h.database.connection.execute(query, params).fetchone()
        return int(row[0])

    assert h.remaining(target) == 0
    assert count("SELECT COUNT(*) FROM messages WHERE thread = ?", (TARGET_THREAD,)) == 0
    assert (
        count("SELECT COUNT(*) FROM messages WHERE update_id IN (?, ?, ?)", (CREATED, 501, 503))
        == 0
    )
    assert (
        count("SELECT COUNT(*) FROM messages WHERE notice_key = ?", (f"ask:open:{CREATED}:0",)) == 0
    )
    assert (
        count(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key IN (?, ?)",
            (f"ask:open:{CREATED}", "expert:command:501"),
        )
        == 0
    )
    assert h.remaining(bystander) > 0
    assert count("SELECT COUNT(*) FROM messages WHERE thread = ?", (BYSTANDER_THREAD,)) > 0
    assert count("SELECT COUNT(*) FROM messages WHERE update_id = ?", (502,)) == 1
    assert (
        count("SELECT COUNT(*) FROM command_replies WHERE reply_key = ?", ("expert:command:502",))
        == 1
    )
    assert count("SELECT COUNT(*) FROM messages WHERE update_id = ?", (504,)) == 1


def test_a_gone_command_reply_ends_only_its_expert_topic(build: Callable[..., Harness]) -> None:
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    bystander = h.open_conversation(CREATED + 100, BYSTANDER_THREAD)
    items = [
        Inbound(501, InboundKind.TEXT, OWNER, GROUP_ID, "501", text="/help", thread=TARGET_THREAD),
        Inbound(
            502, InboundKind.TEXT, OWNER, GROUP_ID, "502", text="/help", thread=BYSTANDER_THREAD
        ),
        Inbound(503, InboundKind.TEXT, OWNER, GROUP_ID, "503", text="/help", thread=77),
    ]
    JournalInbound(h.database).record_batch(CHANNEL, items, next_offset=504, now=NOW)
    asyncio.run(h.turns.handle(items[0]))
    asyncio.run(h.turns.handle(items[1]))
    h.outbox.queue_command_reply(
        [Destination(CHANNEL, str(GROUP_ID), 77)],
        [Message("component reply")],
        key="component:reply:503",
        now=NOW,
    )

    target_reply = h.database.connection.execute(
        "SELECT conversation_id FROM messages WHERE notice_key = 'expert:command:501:0'"
    ).fetchone()
    assert target_reply["conversation_id"] == target

    sender = OutboxSender(
        h.outbox,
        cast("Channel", RefusingThreadChannel(TARGET_THREAD)),
        h.clock,
        interval_s=1,
        gone=h.ending,
    )
    asyncio.run(sender.tick())

    assert h.remaining(target) == 0
    assert not conversation_directory(h.directory, target).exists()
    assert h.remaining(bystander) > 0
    assert conversation_directory(h.directory, bystander).exists()
    assert h.topics.deleted == []
    assert (
        h.database.connection.execute(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key = 'expert:command:501'"
        ).fetchone()[0]
        == 0
    )
    assert (
        h.database.connection.execute(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key = 'expert:command:502'"
        ).fetchone()[0]
        == 1
    )
    assert (
        h.database.connection.execute("SELECT COUNT(*) FROM messages WHERE thread = 77").fetchone()[
            0
        ]
        == 2
    )


def test_closing_a_conversation_stops_a_turn_that_is_still_gathering_its_context(
    build: Callable[..., Harness],
) -> None:
    client = StalledClient()
    expert = AnsweringExpert()
    h = build(expert, client)
    conversation = h.open_conversation(CREATED, TARGET_THREAD)

    async def scenario() -> None:
        running = asyncio.create_task(h.turns.run(conversation, CREATED))
        await asyncio.wait_for(client.started.wait(), timeout=1)
        await asyncio.wait_for(_topic_closed(h, conversation, TARGET_THREAD), timeout=1)
        await asyncio.wait_for(running, timeout=1)

    asyncio.run(scenario())

    assert not expert.started.is_set()
    assert h.remaining(conversation) == 0
    assert not conversation_directory(h.directory, conversation).exists()
    assert h.topics.deleted == [TARGET_THREAD]


def test_a_failed_directory_removal_keeps_the_rows_until_the_sweep_finishes(
    build: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    real_rmtree = shutil.rmtree
    failures: list[OSError] = [OSError("the directory is busy")]

    def flaky_rmtree(path: Path) -> None:
        if failures:
            raise failures.pop()
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    h.topics.refusals = [ChannelRefusedError(TOPIC_GONE_REASON)]

    async def scenario() -> None:
        with pytest.raises(OSError, match="busy"):
            await h.ending.topic_gone(
                conversation, Destination(CHANNEL, str(GROUP_ID), TARGET_THREAD)
            )
        assert h.remaining(conversation) > 0
        await h.ending.sweep()

    asyncio.run(scenario())

    assert h.remaining(conversation) == 0
    assert not conversation_directory(h.directory, conversation).exists()


def test_a_crash_after_the_directory_is_removed_keeps_the_rows_until_the_sweep_finishes(
    build: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    real_erase = h.log.erase
    crashes: list[RuntimeError] = [RuntimeError("crash")]

    def crashing_erase(conversation_id: int, *, topic_chat: int) -> None:
        if crashes:
            raise crashes.pop()
        real_erase(conversation_id, topic_chat=topic_chat)

    monkeypatch.setattr(h.log, "erase", crashing_erase)
    h.topics.refusals = [ChannelRefusedError(TOPIC_GONE_REASON)]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="crash"):
            await h.ending.topic_gone(
                conversation, Destination(CHANNEL, str(GROUP_ID), TARGET_THREAD)
            )
        assert not conversation_directory(h.directory, conversation).exists()
        assert h.remaining(conversation) > 0
        await h.ending.sweep()

    asyncio.run(scenario())

    assert h.remaining(conversation) == 0
