"""Ending an Expert conversation: each way it ends leaves none of its rows, files or topic."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT, chat_and_thread, telegram_place
from shikiri_yaku.adapters.telegram_files import DOWNLOAD_LIMIT_BYTES
from shikiri_yaku.app.expert import OPEN_REPLY
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_runs import ConversationRuns, conversation_directory
from shikiri_yaku.app.expert_sketches import ExpertSketches
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertResult, ExpertSession
from shikiri_yaku.domain.inbound import Inbound, InboundKind, Received
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.place import Place
from shikiri_yaku.journal import Repairs
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.ports import (
    PLACE_GONE_REASON,
    Button,
    ChannelError,
    ChannelRefusedError,
    Expert,
    Message,
    PlaceGoneError,
    Sender,
    ShikiruTask,
)
from tests.support.attachments import FakeDownloads
from tests.support.copies import Platform
from tests.support.copies import deliver as deliver_copies
from tests.support.event_numbers import number_next_event
from tests.support.expert_proposals import expert_proposals
from tests.support.places import FixedPlaces
from tests.support.rooms import FakeRooms

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator
    from pathlib import Path

    from shikiri_yaku.domain.expert import ExpertRequest
    from shikiri_yaku.ports import EscalationView, ShikiruClient

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
_IN_PLACE: Final = "SELECT COUNT(*) FROM messages WHERE place = ?"
OPERATOR_PLACE: Final = Place(CHANNEL, "operator")


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


class Topics(FakeRooms):
    """Deletes topics; each deletion raises the next scripted refusal first."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[int] = []
        self.refusals: list[Exception] = []

    async def delete(self, room: str) -> None:
        if self.refusals:
            raise self.refusals.pop(0)
        if int(room) in self.deleted:
            raise PlaceGoneError(PLACE_GONE_REASON)
        self.deleted.append(int(room))


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

    async def send(self, place: Place, message: Message) -> str:
        del place, message
        raise self._error


class RefusingThreadChannel:
    def __init__(self, thread_id: int) -> None:
        self._thread_id = thread_id

    async def send(self, place: Place, message: Message) -> str:
        del message
        if chat_and_thread(place)[1] == self._thread_id:
            raise PlaceGoneError(PLACE_GONE_REASON)
        return place.id


class Harness:
    def __init__(self, path: Path, expert: Expert, client: Client) -> None:
        self.database = Database.open(":memory:")
        self.log = Conversations(self.database)
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, Reporter()))
        self.clock = Clock()
        self.topics = Topics()
        self.directory = path
        self.runs = ConversationRuns()
        replies = JournalReplies(
            self.database, SignalStore(self.database), OpStore(self.database), channel_name=CHANNEL
        )
        self.final = JournalFinalEdits(self.database, replies, max_text=MESSAGE_TEXT_LIMIT)
        self.ending = ExpertEnding(
            self.log,
            self.runs,
            self.topics,
            self.outbox,
            self.clock,
            places=FixedPlaces([OPERATOR_PLACE]),
            directory=path,
            retention=RETENTION,
            admin_idle=timedelta(minutes=15),
            final=self.final,
        )
        clients = {"api": cast("ShikiruClient", client)}
        self.turns = ExpertTurns(
            self.log,
            self.outbox,
            self.clock,
            expert=expert,
            rooms=self.topics,
            clients=clients,
            scope={"api": path / "api"},
            directory=path,
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
            downloads=FakeDownloads(),
            sketches=ExpertSketches(
                None,
                self.outbox,
                self.clock,
                directory=path,
                rooms=self.topics,
                outbox_interval_s=1,
            ),
            max_text=MESSAGE_TEXT_LIMIT,
            max_file_bytes=DOWNLOAD_LIMIT_BYTES,
        )

    def open_conversation(self, update_id: int, thread: int, *, origin_chat: int = CHAT) -> int:
        """Open a conversation as a creation does, with its directory and one file in it."""
        self.log.create(
            update_id,
            origin=telegram_place(origin_chat),
            text="question",
            scope=["api"],
            now=self.clock.now(),
        )
        self.log.opened(update_id, str(thread))
        row = self.log.get(update_id)
        assert row is not None
        directory = conversation_directory(self.directory, row.id)
        directory.mkdir(parents=True)
        (directory / "notes.txt").write_text("notes", encoding="utf-8")
        return row.id

    def deliver(self, platform: Platform) -> None:
        deliver_copies(self.outbox, self.final, self.clock, platform, gone=self.ending)

    def copy_ref(self, notice_key: str) -> str:
        row = self.database.connection.execute(
            "SELECT platform_message_id FROM messages WHERE notice_key = ?", (notice_key,)
        ).fetchone()
        return str(row["platform_message_id"])

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
    return Inbound(
        id=501,
        key=str(501),
        kind=kind,
        sender=str(OWNER),
        place=telegram_place(CHAT, thread),
        message_ref="501",
        text=text,
    )


async def _topic_closed(h: Harness, conversation: int, thread: int) -> None:
    del conversation
    await h.turns.handle(_inbound(InboundKind.ROOM_CLOSED, thread))


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
        [telegram_place(GROUP_ID, thread)],
        Message("the answer"),
        key="expert:answer:501",
        now=h.clock.now(),
        conversation_id=conversation,
    )
    sender = OutboxSender(
        h.outbox,
        cast("Sender", RefusingChannel(PlaceGoneError(PLACE_GONE_REASON))),
        h.clock,
        interval_s=1,
        gone=h.ending,
    )
    await sender.tick()


_ENDINGS: Final = [_topic_closed, _close_command, _expired, _topic_gone_on_send]
_ENDING_IDS: Final = ["topic-closed", "close-command", "expired", "topic-gone-on-send"]


@pytest.mark.parametrize(
    "end",
    _ENDINGS,
    ids=_ENDING_IDS,
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

    [report] = [item for item in h.outbox.pending() if item.conversation_id is None]
    assert report.place == OPERATOR_PLACE
    assert h.remaining(conversation) == 0
    assert not conversation_directory(h.directory, conversation).exists()
    assert h.topics.deleted == [TARGET_THREAD]


def test_a_topic_row_of_an_earlier_version_is_erased_with_its_conversation(
    build: Callable[..., Harness],
) -> None:
    h = build()
    conversation = h.open_conversation(CREATED, TARGET_THREAD)
    h.database.connection.execute(
        "INSERT INTO topics (thread_id, state, conversation_id) VALUES (?, 'open', ?)",
        (TARGET_THREAD, conversation),
    )

    asyncio.run(h.ending.end(conversation))

    assert h.remaining(conversation) == 0


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
    h.topics.refusals = [PlaceGoneError(PLACE_GONE_REASON)]

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
        Inbound(
            id=CREATED,
            key=str(CREATED),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(PRIVATE_CHAT),
            message_ref=str(CREATED),
            text="/expert question",
        ),
        Inbound(
            id=501,
            key=str(501),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(CHAT, TARGET_THREAD),
            message_ref="501",
            text="/help",
        ),
        Inbound(
            id=502,
            key=str(502),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(CHAT, BYSTANDER_THREAD),
            message_ref="502",
            text="/help",
        ),
        Inbound(
            id=503,
            key=str(503),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(CHAT, TARGET_THREAD),
            message_ref="503",
            text="/close",
        ),
        Inbound(
            id=504,
            key=str(504),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(CHAT, 77),
            message_ref="504",
            text="component",
        ),
    ]
    number_next_event(h.database, CHANNEL, CREATED)
    JournalInbound(h.database).record_batch(CHANNEL, items, cursor="505", now=NOW)
    target = h.open_conversation(CREATED, TARGET_THREAD, origin_chat=PRIVATE_CHAT)
    bystander = h.open_conversation(CREATED + 100, BYSTANDER_THREAD)
    h.outbox.queue_command_reply(
        [Place(CHANNEL, str(PRIVATE_CHAT))],
        [Message("Open the Expert conversation.")],
        key=f"expert:open:{CREATED}",
        now=NOW,
        conversation_id=target,
    )

    async def scenario() -> None:
        await h.turns.handle(items[1])
        await h.turns.handle(items[2])
        await h.turns.handle(items[3])

    asyncio.run(scenario())
    h.deliver(Platform())
    asyncio.run(h.ending.sweep())

    def count(query: str, params: tuple[object, ...]) -> int:
        row = h.database.connection.execute(query, params).fetchone()
        return int(row[0])

    assert h.remaining(target) == 0
    assert count(_IN_PLACE, (telegram_place(GROUP_ID, TARGET_THREAD).id,)) == 0
    assert (
        count("SELECT COUNT(*) FROM messages WHERE update_id IN (?, ?, ?)", (CREATED, 501, 503))
        == 0
    )
    assert (
        count("SELECT COUNT(*) FROM messages WHERE notice_key = ?", (f"expert:open:{CREATED}:0",))
        == 0
    )
    assert (
        count(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key IN (?, ?)",
            (f"expert:open:{CREATED}", "expert:command:501"),
        )
        == 0
    )
    assert h.remaining(bystander) > 0
    assert count(_IN_PLACE, (telegram_place(GROUP_ID, BYSTANDER_THREAD).id,)) > 0
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
        Inbound(
            id=501,
            key=str(501),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(GROUP_ID, TARGET_THREAD),
            message_ref="501",
            text="/help",
        ),
        Inbound(
            id=502,
            key=str(502),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(GROUP_ID, BYSTANDER_THREAD),
            message_ref="502",
            text="/help",
        ),
        Inbound(
            id=503,
            key=str(503),
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(GROUP_ID, 77),
            message_ref="503",
            text="/help",
        ),
    ]
    JournalInbound(h.database).record_batch(CHANNEL, items, cursor="504", now=NOW)
    asyncio.run(h.turns.handle(items[0]))
    asyncio.run(h.turns.handle(items[1]))
    h.outbox.queue_command_reply(
        [telegram_place(GROUP_ID, 77)],
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
        cast("Sender", RefusingThreadChannel(TARGET_THREAD)),
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
    kept = h.database.connection.execute(_IN_PLACE, (telegram_place(GROUP_ID, 77).id,)).fetchone()
    assert kept[0] == 2


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
    h.topics.refusals = [PlaceGoneError(PLACE_GONE_REASON)]

    async def scenario() -> None:
        with pytest.raises(OSError, match="busy"):
            await h.ending.room_gone(conversation, telegram_place(GROUP_ID, TARGET_THREAD))
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

    def crashing_erase(conversation_id: int, *, room_place: Place) -> None:
        if crashes:
            raise crashes.pop()
        real_erase(conversation_id, room_place=room_place)

    monkeypatch.setattr(h.log, "erase", crashing_erase)
    h.topics.refusals = [PlaceGoneError(PLACE_GONE_REASON)]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="crash"):
            await h.ending.room_gone(conversation, telegram_place(GROUP_ID, TARGET_THREAD))
        assert not conversation_directory(h.directory, conversation).exists()
        assert h.remaining(conversation) > 0
        await h.ending.sweep()

    asyncio.run(scenario())

    assert h.remaining(conversation) == 0


def _queue_ask_reply(
    h: Harness, reply: str, conversation: int | None, update_id: int, prefix: str = "expert"
) -> str:
    """Queue the /expert reply of a conversation as its handler does, and return its notice key."""
    if reply == "open":
        key = f"{prefix}:open:{update_id}"
        message = Message(OPEN_REPLY, (Button("Open topic", url="https://t.me/c/1/22"),))
    else:
        key = f"{prefix}:select:{update_id}"
        message = Message("Choose an Expert.", (Button("/expert@bot", data=f"expert:{update_id}"),))
    h.outbox.queue_command_reply(
        [Place(CHANNEL, str(PRIVATE_CHAT))],
        [message],
        key=key,
        now=NOW,
        conversation_id=conversation,
    )
    return f"{key}:0"


@pytest.mark.parametrize("end", _ENDINGS, ids=_ENDING_IDS)
@pytest.mark.parametrize("reply", ["open", "select"])
@pytest.mark.parametrize("prefix", ["expert", "ask"])
def test_each_ending_closes_its_ask_reply_and_no_other(
    build: Callable[..., Harness],
    end: Callable[[Harness, int, int], Awaitable[None]],
    reply: str,
    prefix: str,
) -> None:
    """A reply queued before the rename carries the old prefix; startup renames it before an end."""
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    bystander = h.open_conversation(CREATED + 100, BYSTANDER_THREAD)
    target_key = _queue_ask_reply(h, reply, target, CREATED, prefix)
    bystander_key = _queue_ask_reply(h, "open", bystander, CREATED + 100, prefix)
    h.log.touch(bystander, NOW + timedelta(days=3))
    platform = Platform()
    h.deliver(platform)
    target_ref = h.copy_ref(target_key)
    bystander_ref = h.copy_ref(bystander_key)
    assert Repairs(h.database).rename_keys() == (0 if prefix == "expert" else 4)

    asyncio.run(end(h, target, TARGET_THREAD))
    h.deliver(platform)
    asyncio.run(h.ending.sweep())

    edits = dict(platform.edits)
    closed = edits[target_ref]
    assert closed.text.endswith(h.clock.now().strftime("%Y-%m-%d %H:%M UTC"))
    assert closed.buttons == ()
    assert bystander_ref not in edits
    assert h.remaining(target) == 0
    assert h.remaining(bystander) > 0


@pytest.mark.parametrize(
    ("failure", "erased_at_once"),
    [(ChannelError("offline"), False), (ChannelRefusedError("cannot edit"), True)],
    ids=["retried", "given-up"],
)
def test_the_rows_stay_until_the_closed_reply_is_edited_or_given_up(
    build: Callable[..., Harness],
    *,
    failure: Exception,
    erased_at_once: bool,
) -> None:
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    _queue_ask_reply(h, "open", target, CREATED)
    platform = Platform()
    h.deliver(platform)

    asyncio.run(h.ending.end(target))
    platform.edit_failures.append(failure)
    h.deliver(platform)
    asyncio.run(h.ending.sweep())
    assert (h.remaining(target) == 0) is erased_at_once

    h.deliver(platform)
    asyncio.run(h.ending.sweep())
    assert h.remaining(target) == 0
    assert not conversation_directory(h.directory, target).exists()


def test_a_retried_ending_keeps_the_time_the_conversation_closed(
    build: Callable[..., Harness],
) -> None:
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    _queue_ask_reply(h, "open", target, CREATED)
    platform = Platform()
    h.deliver(platform)

    asyncio.run(h.ending.end(target))
    platform.edit_failures.append(ChannelError("offline"))
    h.deliver(platform)
    h.clock.current = NOW + timedelta(hours=1)
    asyncio.run(h.ending.sweep())
    h.deliver(platform)

    [(_, closed)] = platform.edits
    assert closed.text.endswith("2026-01-01 00:00 UTC")


def test_recovery_after_recording_an_ending_keeps_its_time(
    build: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = build()
    target = h.open_conversation(CREATED, TARGET_THREAD)
    _queue_ask_reply(h, "open", target, CREATED)
    platform = Platform()
    h.deliver(platform)
    settle_notice = h.final.settle_notice

    def crash_before_reply_edit(
        notice_key: str, message: Message, *, conversation_id: int | None = None
    ) -> None:
        del notice_key, message, conversation_id
        failure = "crash"
        raise RuntimeError(failure)

    monkeypatch.setattr(h.final, "settle_notice", crash_before_reply_edit)
    with pytest.raises(RuntimeError, match="crash"):
        asyncio.run(h.ending.end(target))

    row = h.log.conversation(target)
    assert row is not None
    assert row.state == "closing"
    assert row.ending_at == NOW

    monkeypatch.setattr(h.final, "settle_notice", settle_notice)
    h.clock.current = NOW + timedelta(hours=1)
    asyncio.run(h.ending.sweep())
    h.deliver(platform)

    [(_, closed)] = platform.edits
    assert closed.text.endswith("2026-01-01 00:00 UTC")


def test_closing_associates_an_untagged_shared_selection_reply(
    build: Callable[..., Harness],
) -> None:
    h = build()
    selection_key = _queue_ask_reply(h, "select", None, CREATED)
    platform = Platform()
    h.deliver(platform)
    selection_ref = h.copy_ref(selection_key)
    target = h.open_conversation(CREATED, TARGET_THREAD)
    assert (
        h.database.connection.execute(
            "SELECT conversation_id FROM messages WHERE notice_key = ?", (selection_key,)
        ).fetchone()["conversation_id"]
        is None
    )

    asyncio.run(h.ending.end(target))
    associated = h.database.connection.execute(
        "SELECT conversation_id FROM messages WHERE notice_key = ?", (selection_key,)
    ).fetchone()
    assert associated["conversation_id"] == target
    assert h.final.pending_for(target)
    assert h.remaining(target) > 0

    h.deliver(platform)
    assert dict(platform.edits)[selection_ref].text.endswith("2026-01-01 00:00 UTC")
    asyncio.run(h.ending.sweep())
    assert h.remaining(target) == 0


def _conversation_row(database: Database, update_id: int) -> int:
    row = database.connection.execute(
        "SELECT id FROM conversations WHERE update_id = ?", (update_id,)
    ).fetchone()
    return int(row[0])


def test_erasing_an_event_keeps_the_reply_keys_that_only_end_in_the_same_number() -> None:
    database = Database.open(":memory:")
    try:
        conversations = Conversations(database)
        outbox = JournalOutbox(database, MessageStore(database, Reporter()))
        asked = Received(
            key="900",
            kind=InboundKind.TEXT,
            sender=str(OWNER),
            place=telegram_place(CHAT, TARGET_THREAD),
            message_ref="900",
            text="/help",
        )
        JournalInbound(database).record_batch(CHANNEL, [asked], "901", NOW)
        [stored] = JournalInbound(database).pending()
        conversations.create(stored.id, origin=telegram_place(CHAT), text="hi", scope=[], now=NOW)
        others = {f"submit:{stored.id}", f"expert:view:700:{stored.id}"}
        for key in (f"expert:command:{stored.id}", *others):
            outbox.queue_command_reply([], [], key=key, now=NOW)

        conversations.erase(
            _conversation_row(database, stored.id),
            room_place=telegram_place(CHAT, TARGET_THREAD),
        )

        kept = {
            row[0] for row in database.connection.execute("SELECT reply_key FROM command_replies")
        }
        assert kept == others
    finally:
        database.close()


def test_erasing_a_conversation_deletes_the_reply_keys_of_its_own_replies_only() -> None:
    database = Database.open(":memory:")
    try:
        conversations = Conversations(database)
        outbox = JournalOutbox(database, MessageStore(database, Reporter()))
        for update_id, thread in ((CREATED, TARGET_THREAD), (CREATED + 100, BYSTANDER_THREAD)):
            conversations.create(
                update_id, origin=telegram_place(CHAT), text="hi", scope=[], now=NOW
            )
            conversations.opened(update_id, str(thread))
        target = _conversation_row(database, CREATED)
        bystander = _conversation_row(database, CREATED + 100)
        place = [telegram_place(CHAT, TARGET_THREAD)]
        outbox.queue_command_reply(
            place,
            [Message("a"), Message("b")],
            key="expert:view:601:1",
            now=NOW,
            conversation_id=target,
        )
        outbox.queue_command_reply(
            place, [Message("c")], key="expert:view:601", now=NOW, conversation_id=target
        )
        outbox.queue_command_reply(
            place,
            [Message("d")],
            key="expert:view:602:1",
            now=NOW,
            conversation_id=bystander,
        )
        outbox.queue_command_reply(place, [Message("e")], key="expert:select:603", now=NOW)

        conversations.erase(target, room_place=place[0])

        kept = {
            row[0] for row in database.connection.execute("SELECT reply_key FROM command_replies")
        }
        assert kept == {"expert:view:602:1", "expert:select:603"}
    finally:
        database.close()
