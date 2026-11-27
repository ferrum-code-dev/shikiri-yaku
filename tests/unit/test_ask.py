"""Expert conversation opening and crash recovery without Telegram or agents."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT
from shikiri_yaku.adapters.telegram_files import DOWNLOAD_LIMIT_BYTES
from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.expert import Ask
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_ladder import LadderExpert
from shikiri_yaku.app.expert_runs import ConversationRuns, conversation_directory
from shikiri_yaku.app.expert_sketches import ExpertSketches
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.expert import (
    MODE_ADMIN,
    MODE_EXPERT,
    AdminAnswer,
    ExpertAnswer,
    ExpertResult,
    ExpertSession,
    stored_answer,
)
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import (
    PLACE_GONE_REASON,
    ChannelError,
    ChannelRefusedError,
    ExpertError,
    PlaceGoneError,
    ShikiruTask,
)
from tests.support.attachments import FakeDownloads
from tests.support.destinations import FixedDestinations
from tests.support.expert_proposals import expert_proposals
from tests.support.rooms import FakeRooms, FakeSharedChat

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from pathlib import Path

    from shikiri_yaku.adapters.telegram import BotApi
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.replies import Replies
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.expert import ExpertRequest
    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.journal.replies import JournalReplies
    from shikiri_yaku.ports import EscalationView, Inbox, Message, ShikiruClient

NOW = datetime(2026, 1, 1, tzinfo=UTC)
_RETRY_LATER = "retry later"
_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def close_databases() -> Generator[None]:
    yield
    for database in _DATABASES:
        database.close()
    _DATABASES.clear()


class Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Reporter:
    def report(self, line: str) -> None:
        raise AssertionError(line)


class Client:
    async def list_tasks(self, statuses: object = None) -> list[ShikiruTask]:  # noqa: ARG002
        return []

    async def list_escalations(self) -> list[EscalationView]:
        return []


class Topics(FakeRooms):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[str] = []
        self.renamed: list[str] = []
        self.refuse_rename = False

    async def open(self, title: str) -> str:
        self.created.append(title)
        return str(21 + len(self.created))

    async def rename(self, room: str, title: str) -> None:  # noqa: ARG002
        self.renamed.append(title)
        if self.refuse_rename:
            message = "refused"
            raise ChannelRefusedError(message)


class Expert:
    def __init__(self) -> None:
        self.requests: list[ExpertRequest] = []
        self.crash = False
        self.fail = False

    async def turn(self, request: ExpertRequest) -> ExpertResult:
        self.requests.append(request)
        if self.crash:
            raise KeyboardInterrupt
        if self.fail:
            message = "unavailable"
            raise ExpertError(message)
        answer = ExpertAnswer(reply="answer", title="title", tasks=[], findings="facts")
        return ExpertResult(answer, ExpertSession(0, "fake", "session", "facts"), None)


class Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[Destination, Message]] = []

    async def send(self, destination: Destination, message: Message) -> str:
        self.sent.append((destination, message))
        return f"{destination.chat}/{len(self.sent)}"

    async def edit(self, message_ref: str, message: Message) -> None:
        raise NotImplementedError

    async def delete(self, message_ref: str) -> None:
        raise NotImplementedError


class RefusingOriginTopic(Channel):
    async def send(self, destination: Destination, message: Message) -> str:
        del message
        if destination.thread == 77:
            raise PlaceGoneError(PLACE_GONE_REASON)
        raise ChannelError(_RETRY_LATER)


class Harness:
    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        group: bool = True,
        shared: bool = False,
        expert: Expert | None = None,
        clients: dict[str, ShikiruClient] | None = None,
        admins: frozenset[int] = frozenset(),
        turn_admins: frozenset[int] | None = None,
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        self.database = Database.open(":memory:")
        _DATABASES.append(self.database)
        self.log = Conversations(self.database)
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, Reporter()))
        self.topics = Topics()
        self.expert = expert if expert is not None else Expert()
        self.runs = ConversationRuns()
        self.ending = ExpertEnding(
            self.log,
            self.runs,
            self.topics,
            self.outbox,
            Clock(),
            places=FixedDestinations(),
            directory=path,
            retention=timedelta(days=14),
            admin_idle=timedelta(minutes=15),
            final=JournalFinalEdits(
                self.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT
            ),
        )
        clients = clients or {name: cast("ShikiruClient", Client()) for name in ("api", "web")}
        self.turns = ExpertTurns(
            self.log,
            self.outbox,
            Clock(),
            expert=self.expert,
            rooms=self.topics,
            clients=clients,
            scope={name: path / name for name in ("api", "web")},
            directory=path,
            channel="telegram",
            runs=self.runs,
            ending=self.ending,
            downloads=FakeDownloads(),
            sketches=ExpertSketches(
                None,
                self.outbox,
                Clock(),
                directory=path,
                rooms=self.topics,
                outbox_interval_s=1,
            ),
            proposals=expert_proposals(
                self.database,
                self.log,
                self.outbox,
                Clock(),
                clients,
                channel="telegram",
                group_id=-100123,
            ),
            admins=admins if turn_admins is None else turn_admins,
            known_secrets=known_secrets,
            max_text=MESSAGE_TEXT_LIMIT,
            max_file_bytes=DOWNLOAD_LIMIT_BYTES,
        )
        self.ask = Ask(
            self.log,
            self.outbox,
            Clock(),
            configured=enabled,
            rooms=self.topics if group else None,
            shared=FakeSharedChat() if shared else None,
            turns=self.turns,
            scope={name: path / name for name in ("api", "web")},
            channel="telegram",
            final=JournalFinalEdits(
                self.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT
            ),
            admins=admins,
            max_text=MESSAGE_TEXT_LIMIT,
        )
        self.commands = Commands(
            self.outbox,
            Clock(),
            cast("Status", None),
            cast("OpenCommand", None),
            cast("GoalCommand", None),
            destinations=FixedDestinations(topics={77: "api"}),
            channel="telegram",
            name_max_chars=40,
            shared=FakeSharedChat() if shared else None,
            ask=self.ask,
            max_text=MESSAGE_TEXT_LIMIT,
        )

    def send(self, text: str = "/expert hello", thread: int | None = None) -> None:
        asyncio.run(
            self.commands.handle(
                Inbound(1, InboundKind.TEXT, 5, -100123, "1", text=text, thread=thread)
            )
        )


@pytest.mark.parametrize(("thread", "expected"), [(None, {"api", "web"}), (77, {"api"})])
def test_scope_and_duplicate(tmp_path: Path, thread: int | None, expected: set[str]) -> None:
    h = Harness(tmp_path)
    h.send(thread=thread)
    h.send(thread=thread)
    assert set(h.expert.requests[0].scope) == expected
    assert len(h.expert.requests) == len(h.topics.created) == 1
    channel = Channel()
    asyncio.run(OutboxSender(h.outbox, channel, Clock(), interval_s=1).tick())
    assert len(channel.sent) == 2
    assert channel.sent[0][1].buttons[0].url == "https://t.me/c/123/22"
    assert channel.sent[1][0].thread == 22
    assert channel.sent[1][1].text == "answer"


@pytest.mark.parametrize(
    ("enabled", "group", "text", "shared", "thread", "replies"),
    [
        (False, True, "/expert hello", False, None, 1),
        (True, False, "/expert hello", False, None, 1),
        (True, True, "/expert", False, None, 1),
        (True, True, "/expert hello", True, None, 1),
        (True, True, "/expert@bot hello", True, None, 2),
        (True, True, "/expert@another hello", True, None, 0),
        (True, True, "/expert hello", True, 77, 2),
        (True, True, "/expert hello", True, 78, 0),
    ],
)
def test_refusals_and_shared_group(  # noqa: PLR0917 -- table columns
    tmp_path: Path,
    enabled: bool,  # noqa: FBT001 -- table column
    group: bool,  # noqa: FBT001 -- table column
    text: str,
    shared: bool,  # noqa: FBT001 -- table column
    thread: int | None,
    replies: int,
) -> None:
    h = Harness(tmp_path, enabled=enabled, group=group, shared=shared)
    h.send(text, thread)
    pending = h.outbox.pending()
    assert len(pending) == replies
    if replies == 1 and shared and text == "/expert hello" and thread is None:
        assert pending[0].message.reply_to == "1"
        assert pending[0].message.buttons[0].label == "/expert@bot"
    elif replies == 1:
        reason = "configured" if not enabled else "group" if not group else "question"
        assert reason in pending[0].message.text.lower()
    assert len(h.topics.created) == (1 if replies == 2 else 0)


def test_crash_before_topic_is_uncertain(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.log.create(1, chat=-100123, thread=None, text="hello", scope=["api"], now=NOW)
    h.turns.recover()
    h.send()
    assert not h.topics.created
    assert not h.expert.requests
    assert len(h.outbox.pending()) == 1


def test_crash_after_topic_resumes_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.log.create(1, chat=-100123, thread=None, text="hello", scope=["api"], now=NOW)
    h.log.opened(1, "22")
    h.turns.recover()
    h.send()
    h.send()
    assert not h.topics.created
    assert len(h.expert.requests) == 1


def test_crash_during_turn_is_not_repeated(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.expert.crash = True
    with pytest.raises(KeyboardInterrupt):
        h.send()
    h.turns.recover()
    h.expert.crash = False
    h.send()
    assert len(h.topics.created) == len(h.expert.requests) == 1
    assert len(h.outbox.pending()) == 2


def test_unavailable_expert_is_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.expert.fail = True
    h.send()
    assert len(h.expert.requests) == 1
    assert len(h.outbox.pending()) == 2


class WaitingExpert(Expert):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def turn(self, request: ExpertRequest) -> ExpertResult:
        self.requests.append(request)
        if len(self.requests) == 2:
            self.started.set()
        await self.release.wait()
        answer = ExpertAnswer(reply="answer", title="title", tasks=[], findings="facts")
        return ExpertResult(answer, ExpertSession(0, "fake", "session", "facts"), None)


def test_independent_asks_run_concurrently_and_are_not_scheduled_twice(tmp_path: Path) -> None:
    async def scenario() -> None:
        expert = WaitingExpert()
        h = Harness(tmp_path, expert=expert)

        class InboundLog(JournalInbound):
            def __init__(self, database: Database) -> None:
                super().__init__(database)
                self.completed = asyncio.Event()
                self.count = 0

            def mark_handled(self, update_id: int) -> None:
                super().mark_handled(update_id)
                self.count += 1
                if self.count == 2:
                    self.completed.set()

        log = InboundLog(h.database)
        items = [
            Inbound(update, InboundKind.TEXT, 5, -100123, str(update), text="/expert hello")
            for update in (1, 2)
        ]
        log.record_batch("telegram", items, 3, NOW)

        class Poller:
            async def poll_once(self) -> None:
                return None

        class Health:
            def source_recovered(self, scope: str) -> None:  # noqa: ARG002
                return None

        service = InboundService(
            cast("InboundPoller", Poller()),
            log,
            cast("Replies", h.commands),
            cast("HealthSource", Health()),
            Clock(),
            interval_s=1,
        )
        await service.tick()
        await asyncio.wait_for(expert.started.wait(), timeout=1)
        await service.tick()
        assert len(h.topics.created) == len(expert.requests) == 2
        assert len(log.pending()) == 2
        expert.release.set()
        await asyncio.wait_for(log.completed.wait(), timeout=1)
        await service.tick()
        assert not log.pending()
        assert len(expert.requests) == 2

    asyncio.run(scenario())


def test_expert_topics_do_not_replace_component_topics(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    topics = TopicStore(h.database)
    topics.save("api", 77, "open")
    h.send()
    topics.save("api", 78, "open")
    topic = topics.get("api")
    assert topic is not None
    assert topic.thread_id == 78
    assert topics.by_thread(22) is None
    assert len(topics.all()) == 1


def test_open_work_is_given_only_for_the_scope(tmp_path: Path) -> None:
    class WorkClient(Client):
        async def list_tasks(
            self,
            statuses: object = None,  # noqa: ARG002 -- Shikiru port
        ) -> list[ShikiruTask]:
            return [ShikiruTask(id="T-1", parent_id=None, status="running", title="work")]

    clients = {"api": cast("ShikiruClient", WorkClient()), "web": cast("ShikiruClient", Client())}
    h = Harness(tmp_path, clients=clients)
    h.send(thread=77)
    context = json.loads(h.expert.requests[0].context)
    assert set(context) == {"api"}
    assert context["api"]["tasks"][0]["id"] == "T-1"


def test_expert_answer_is_linked_to_its_conversation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.send()
    row = h.database.connection.execute(
        "SELECT purpose, conversation_id FROM messages WHERE notice_key = 'expert:answer:1'"
    ).fetchone()
    assert row["purpose"] == "expert"
    conversation = h.log.get(1)
    assert conversation is not None
    assert row["conversation_id"] == conversation.id


def test_a_missing_ask_origin_topic_does_not_end_the_expert_topic(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(
        h.ask.handle(
            Inbound(
                1,
                InboundKind.TEXT,
                5,
                -100123,
                "1",
                text="/expert hello",
                thread=77,
            ),
            "api",
        )
    )
    conversation = h.log.get(1)
    assert conversation is not None

    sender = OutboxSender(h.outbox, RefusingOriginTopic(), Clock(), interval_s=1, gone=h.ending)
    asyncio.run(sender.tick())

    remaining = h.log.get(1)
    assert remaining is not None
    assert remaining.room == "22"
    assert conversation_directory(tmp_path, remaining.id).exists()


def test_private_origin_interrupted_turn_is_reported_in_the_group_topic(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.log.create(1, chat=5, thread=None, text="hello", scope=["api"], now=NOW)
    h.log.opened(1, "22")
    assert h.log.start(1)
    h.turns.recover()

    [pending] = h.outbox.pending()
    assert pending.destination.chat == "-100123"
    assert pending.destination.thread == 22
    assert "interrupted" in pending.message.text


def test_refused_topic_rename_does_not_prevent_answer(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.topics.refuse_rename = True
    h.send()
    h.send()

    pending = h.outbox.pending()
    answers = [item for item in pending if item.destination.thread == 22]
    assert len(answers) == 1
    assert answers[0].message.text == "answer"
    assert len(h.expert.requests) == 1
    assert len(h.topics.created) == 1


def _ask(update_id: int, message_ref: str) -> Inbound:
    return Inbound(
        update_id, InboundKind.TEXT, 5, -100123, message_ref, text=f"/expert question {update_id}"
    )


def test_each_ask_reply_names_its_own_command_and_opens_its_own_topic(tmp_path: Path) -> None:
    async def scenario(h: Harness) -> None:
        await asyncio.gather(
            h.ask.handle(_ask(1, "-100123/71"), None),
            h.ask.handle(_ask(2, "-100123/72"), None),
        )

    h = Harness(tmp_path)
    asyncio.run(scenario(h))

    buttons = [item for item in h.outbox.pending() if item.message.buttons]
    assert sorted(item.message.reply_to or "" for item in buttons) == [
        "-100123/71",
        "-100123/72",
    ]
    for update_id, command in ((1, "-100123/71"), (2, "-100123/72")):
        [button] = [item for item in buttons if item.message.reply_to == command]
        conversation = h.log.get(update_id)
        assert conversation is not None
        assert conversation.room is not None
        assert button.message.buttons[0].url == h.topics.link(conversation.room)


class FlakyChannel(Channel):
    """Fails the first send with a transient error, then accepts every send."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send(self, destination: Destination, message: Message) -> str:
        if not self.failed:
            self.failed = True
            raise ChannelError(_RETRY_LATER)
        return await super().send(destination, message)


def test_the_reply_target_survives_a_failed_send(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(h.ask.handle(_ask(1, "-100123/71"), None))

    channel = FlakyChannel()
    sender = OutboxSender(h.outbox, channel, Clock(), interval_s=1)
    asyncio.run(sender.tick())
    asyncio.run(sender.tick())

    [button] = [message for _, message in channel.sent if message.buttons]
    assert button.reply_to == "-100123/71"


class _BotApi:
    """Answers getUpdates with one scripted payload."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:  # noqa: ARG002
        self.calls.append(method)
        assert method == "getUpdates"
        return self._payload


def _general_payload(text: str) -> dict[str, object]:
    return {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "message_id": 91,
                    "from": {"id": 5},
                    "chat": {"id": -100123},
                    "message_thread_id": 1,
                    "text": text,
                },
            }
        ],
    }


def _pump(h: Harness, payload: Mapping[str, object]) -> None:
    async def scenario() -> None:
        done = asyncio.Event()
        updates = TelegramUpdates(cast("BotApi", _BotApi(payload)), group_id=-100123)
        log = JournalInbound(h.database)
        poller = InboundPoller(updates, log, Clock(), channel="telegram", owners=[5], wait_s=1)
        service = InboundService(
            poller,
            log,
            cast("Replies", _Signalled(h.commands, done)),
            cast("HealthSource", _NoHealth()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await service.tick()
        await asyncio.wait_for(done.wait(), timeout=1)
        await service.tick()

    asyncio.run(scenario())


class _NoHealth:
    def source_recovered(self, scope: str) -> None:  # noqa: ARG002
        return None


class _Signalled:
    def __init__(self, inner: Commands, done: asyncio.Event) -> None:
        self._inner = inner
        self._done = done

    async def handle(self, item: Inbound) -> None:
        await self._inner.handle(item)
        self._done.set()


@pytest.mark.parametrize(
    ("shared", "text"),
    [(False, "/expert hello"), (True, "/expert@bot hello"), (True, "/expert hello")],
)
def test_ask_in_general_through_the_inbound_service(
    tmp_path: Path,
    shared: bool,  # noqa: FBT001 -- table column
    text: str,
) -> None:
    h = Harness(tmp_path, shared=shared)
    _pump(h, _general_payload(text))

    opened = text == "/expert@bot hello" or not shared
    assert len(h.topics.created) == (1 if opened else 0)
    assert len(h.expert.requests) == (1 if opened else 0)
    pending = h.outbox.pending()
    assert len(pending) == (2 if opened else 1)
    if opened:
        [button] = [item for item in pending if item.message.buttons]
        assert button.destination.thread is None
        assert button.message.reply_to == "-100123/91"
        assert button.message.buttons[0].url == "https://t.me/c/123/22"


def test_selection_opens_once_and_edits_the_reply(tmp_path: Path) -> None:
    h = Harness(tmp_path, shared=True)
    original = _ask(1, "-100123/71")
    inbound = JournalInbound(h.database)
    inbound.record_batch("telegram", [original], 2, NOW)
    asyncio.run(h.commands.handle(original))
    asyncio.run(h.commands.handle(original))
    [offer] = h.outbox.pending()
    assert offer.message.reply_to == original.message_ref
    assert offer.message.buttons[0].label == "/expert@bot"
    channel = Channel()
    asyncio.run(OutboxSender(h.outbox, channel, Clock(), interval_s=1).tick())
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    forged = Inbound(3, InboundKind.TAP, 5, -100123, "-100123/999", data="expert:1")
    assert not asyncio.run(h.commands.tap(forged))
    assert asyncio.run(h.commands.tap(tap))
    assert asyncio.run(h.commands.tap(tap))
    assert len(h.topics.created) == len(h.expert.requests) == 1
    final = JournalFinalEdits(h.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT)
    [edit] = final.to_edit()
    assert edit.message_ref == tap.message_ref
    assert edit.message.buttons[0].url == "https://t.me/c/123/22"


def test_a_selection_from_an_ask_sent_as_a_reply_opens_once(tmp_path: Path) -> None:
    h = Harness(tmp_path, shared=True)
    original = replace(
        _ask(1, "-100123/71"),
        kind=InboundKind.REPLY,
        reply_to_ref="-100123/70",
    )
    inbound = JournalInbound(h.database)
    inbound.record_batch("telegram", [original], 2, NOW)
    asyncio.run(h.commands.handle(original))
    [offer] = h.outbox.pending()
    assert offer.message.buttons[0].label == "/expert@bot"
    channel = Channel()
    asyncio.run(OutboxSender(h.outbox, channel, Clock(), interval_s=1).tick())
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(h.commands.tap(tap))
    assert asyncio.run(h.commands.tap(tap))
    assert len(h.topics.created) == len(h.expert.requests) == 1


def test_selection_recovers_after_topic_is_opened(tmp_path: Path) -> None:
    h = Harness(tmp_path, shared=True)
    original = _ask(1, "-100123/71")
    JournalInbound(h.database).record_batch("telegram", [original], 2, NOW)
    asyncio.run(h.commands.handle(original))
    channel = FlakyChannel()
    sender = OutboxSender(h.outbox, channel, Clock(), interval_s=1)
    asyncio.run(sender.tick())
    assert len(h.outbox.pending()) == 1
    asyncio.run(sender.tick())
    assert channel.sent[0][1].reply_to == original.message_ref
    h.log.create(1, chat=-100123, thread=None, text="question 1", scope=["api", "web"], now=NOW)
    h.log.opened(1, "22")
    h.turns.recover()
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(h.commands.tap(tap))
    assert not h.topics.created
    assert len(h.expert.requests) == 1
    final = JournalFinalEdits(h.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT)
    assert len(final.to_edit()) == 1


def test_shared_instances_offer_independently(tmp_path: Path) -> None:
    first = Harness(tmp_path / "first", shared=True)
    second = Harness(tmp_path / "second", shared=True)
    original = _ask(1, "-100123/71")
    for instance in (first, second):
        JournalInbound(instance.database).record_batch("telegram", [original], 2, NOW)
        asyncio.run(instance.commands.handle(original))
        asyncio.run(OutboxSender(instance.outbox, Channel(), Clock(), interval_s=1).tick())
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(first.commands.tap(tap))
    assert len(first.expert.requests) == 1
    assert not second.expert.requests
    assert not second.topics.created


def test_selection_rejects_non_owner_before_delivery(tmp_path: Path) -> None:
    h = Harness(tmp_path, shared=True)
    original = _ask(1, "-100123/71")
    inbound = JournalInbound(h.database)
    inbound.record_batch("telegram", [original], 2, NOW)
    asyncio.run(h.commands.handle(original))
    asyncio.run(OutboxSender(h.outbox, Channel(), Clock(), interval_s=1).tick())
    tap = Inbound(2, InboundKind.TAP, 99, -100123, "-100123/1", data="expert:1")

    class FakeUpdates:
        async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002
            return Batch(2, (tap,))

    poller = InboundPoller(
        cast("Inbox", FakeUpdates()), inbound, Clock(), channel="telegram", owners=[5], wait_s=1
    )
    asyncio.run(poller.poll_once())
    assert all(item.update_id != 2 for item in inbound.pending())
    assert not h.topics.created


def test_unconfigured_expert_offers_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, enabled=False, shared=True)
    h.send()
    assert not h.outbox.pending()
    assert not h.topics.created


def test_selection_turns_do_not_block_inbound_or_each_other(tmp_path: Path) -> None:  # noqa: C901
    async def scenario() -> None:  # noqa: C901
        expert = WaitingExpert()
        h = Harness(tmp_path, shared=True, expert=expert)

        class InboundLog(JournalInbound):
            def __init__(self, database: Database) -> None:
                super().__init__(database)
                self.completed = asyncio.Event()
                self.count = 0

            def mark_handled(self, update_id: int) -> None:
                super().mark_handled(update_id)
                if update_id >= 3:
                    self.count += 1
                    if self.count == 3:
                        self.completed.set()

        log = InboundLog(h.database)
        originals = [_ask(1, "-100123/71"), _ask(2, "-100123/72")]
        log.record_batch("telegram", originals, 3, NOW)
        for original in originals:
            await h.commands.handle(original)
            log.mark_handled(original.update_id)
        await OutboxSender(h.outbox, Channel(), Clock(), interval_s=1).tick()
        items = [
            Inbound(3, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1"),
            Inbound(4, InboundKind.TAP, 5, -100123, "-100123/2", data="expert:2"),
            Inbound(5, InboundKind.TEXT, 5, -100123, "-100123/73", text="hello"),
        ]
        log.record_batch("telegram", items, 6, NOW)

        class Poller:
            def __init__(self) -> None:
                self.calls = 0

            async def poll_once(self) -> None:
                self.calls += 1

        class Handler:
            def __init__(self) -> None:
                self.ordinary = False

            async def handle(self, item: Inbound) -> None:
                if item.kind == InboundKind.TAP:
                    assert await h.commands.tap(item)
                else:
                    self.ordinary = True

        poller = Poller()
        handler = Handler()
        service = InboundService(
            cast("InboundPoller", poller),
            log,
            cast("Replies", handler),
            cast("HealthSource", _NoHealth()),
            Clock(),
            interval_s=1,
        )
        await asyncio.wait_for(service.tick(), timeout=1)
        await asyncio.wait_for(expert.started.wait(), timeout=1)
        await asyncio.wait_for(service.tick(), timeout=1)
        assert handler.ordinary
        assert poller.calls == 2
        assert len(h.topics.created) == len(expert.requests) == 2
        assert len(log.pending()) == 2
        expert.release.set()
        await asyncio.wait_for(log.completed.wait(), timeout=1)
        await service.tick()
        assert not log.pending()
        assert len(expert.requests) == 2

    asyncio.run(scenario())


ADMIN = 5
OTHER_OWNER = 6


def _admin_text(h: Harness, text: str, sender: int, update_id: int = 1) -> None:
    item = Inbound(update_id, InboundKind.TEXT, sender, -100123, str(update_id), text=text)
    asyncio.run(h.commands.handle(item))


def _sent_texts(h: Harness) -> list[str]:
    channel = Channel()
    asyncio.run(OutboxSender(h.outbox, channel, Clock(), interval_s=1).tick())
    return [message.text for _, message in channel.sent]


@pytest.mark.parametrize(
    ("admins", "sender", "opened"),
    [
        (frozenset[int](), ADMIN, False),
        (frozenset({ADMIN}), OTHER_OWNER, False),
        (frozenset({ADMIN}), ADMIN, True),
    ],
)
def test_admin_opens_only_for_a_listed_admin(
    tmp_path: Path,
    admins: frozenset[int],
    sender: int,
    opened: bool,  # noqa: FBT001
) -> None:
    h = Harness(tmp_path, admins=admins)
    _admin_text(h, "/admin hello", sender)
    assert bool(h.topics.created) is opened
    assert bool(h.expert.requests) is opened
    assert len(h.outbox.pending()) == (3 if opened else 1)


def test_admin_conversation_warns_uses_admin_answers_and_survives_duplicates(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, admins=frozenset({ADMIN}))
    _admin_text(h, "/admin hello", ADMIN)
    _admin_text(h, "/admin hello", ADMIN)
    [request] = h.expert.requests
    assert request.admin
    assert len(h.topics.created) == 1
    conversation = h.log.get(1)
    assert conversation is not None
    assert conversation.mode == MODE_ADMIN
    assert any("not private" in text for text in _sent_texts(h))


def test_ordinary_ask_stays_an_expert_conversation(tmp_path: Path) -> None:
    h = Harness(tmp_path, admins=frozenset({ADMIN}))
    _admin_text(h, "/expert hello", ADMIN)
    [request] = h.expert.requests
    assert not request.admin
    conversation = h.log.get(1)
    assert conversation is not None
    assert conversation.mode == MODE_EXPERT
    assert not any("not private" in text for text in _sent_texts(h))


def test_only_a_listed_admin_takes_part_in_an_admin_topic(tmp_path: Path) -> None:
    h = Harness(tmp_path, admins=frozenset({ADMIN}))
    _admin_text(h, "/admin hello", ADMIN)
    topic = h.log.get(1)
    assert topic is not None
    assert topic.room is not None
    for update_id, sender in ((2, OTHER_OWNER), (3, ADMIN)):
        item = Inbound(
            update_id, InboundKind.TEXT, sender, -100123, str(update_id),
            text="more", thread=int(topic.room),
        )  # fmt: skip
        asyncio.run(h.turns.handle(item))
        asyncio.run(h.turns.handle(item))
    assert [request.message for request in h.expert.requests] == ["hello", "more"]


def test_a_removed_admin_cannot_continue_an_admin_topic(tmp_path: Path) -> None:
    h = Harness(tmp_path, admins=frozenset({ADMIN}), turn_admins=frozenset({OTHER_OWNER}))
    _admin_text(h, "/admin hello", ADMIN)
    topic = h.log.get(1)
    assert topic is not None
    assert topic.room is not None
    item = Inbound(2, InboundKind.TEXT, ADMIN, -100123, "2", text="more", thread=int(topic.room))
    asyncio.run(h.turns.handle(item))
    assert len(h.expert.requests) == 1


def test_a_shared_group_selection_of_admin_needs_an_admin_tap(tmp_path: Path) -> None:
    h = Harness(tmp_path, shared=True, admins=frozenset({ADMIN}))
    original = Inbound(1, InboundKind.TEXT, ADMIN, -100123, "-100123/71", text="/admin hello")
    JournalInbound(h.database).record_batch("telegram", [original], 2, NOW)
    asyncio.run(h.commands.handle(original))
    [offer] = h.outbox.pending()
    assert offer.message.buttons[0].label == "/admin@bot"
    asyncio.run(OutboxSender(h.outbox, Channel(), Clock(), interval_s=1).tick())
    stranger = Inbound(2, InboundKind.TAP, OTHER_OWNER, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(h.commands.tap(stranger))
    assert not h.topics.created
    tap = Inbound(3, InboundKind.TAP, ADMIN, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(h.commands.tap(tap))
    assert asyncio.run(h.commands.tap(tap))
    assert len(h.topics.created) == len(h.expert.requests) == 1


class _Later(Clock):
    def now(self) -> datetime:
        return NOW + timedelta(minutes=16)


class _DeletableTopics(Topics):
    async def delete(self, room: str) -> None:
        del room


def test_an_idle_admin_conversation_ends_but_an_expert_one_does_not(tmp_path: Path) -> None:
    h = Harness(tmp_path, admins=frozenset({ADMIN}))
    _admin_text(h, "/admin hello", ADMIN, 1)
    _admin_text(h, "/expert hello", ADMIN, 2)
    ending = ExpertEnding(
        h.log,
        h.runs,
        _DeletableTopics(),
        h.outbox,
        _Later(),
        places=FixedDestinations(),
        directory=tmp_path,
        retention=timedelta(days=14),
        admin_idle=timedelta(minutes=15),
        final=JournalFinalEdits(
            h.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT
        ),
    )
    asyncio.run(ending.sweep())
    admin, expert = h.log.get(1), h.log.get(2)
    assert admin is None or admin.state != "open"
    assert expert is not None
    assert expert.state == "open"


def test_a_command_in_an_admin_answer_is_stored_but_never_acted_on(tmp_path: Path) -> None:
    class Proposing(Expert):
        async def turn(self, request: ExpertRequest) -> ExpertResult:
            self.requests.append(request)
            answer = AdminAnswer(
                reply="answer", title="t", tasks=[], findings="f", command="ls", reason="look"
            )
            return ExpertResult(answer, ExpertSession(0, "fake", "session", "f"), None)

    h = Harness(tmp_path, admins=frozenset({ADMIN}), expert=Proposing())
    _admin_text(h, "/admin hello", ADMIN)
    assert "answer" in _sent_texts(h)
    assert all(
        not message.buttons or message.buttons[0].url
        for message in (pending.message for pending in h.outbox.pending())
    )


@pytest.mark.parametrize("command", ["/admin", "/admin@bot"])
def test_an_unlisted_owner_gets_no_reply_to_admin_in_a_shared_group(
    tmp_path: Path, command: str
) -> None:
    listed = Harness(tmp_path / "listed", shared=True, admins=frozenset({ADMIN}))
    other = Harness(tmp_path / "other", shared=True, admins=frozenset({OTHER_OWNER}))
    unconfigured = Harness(tmp_path / "none", shared=True)
    original = Inbound(1, InboundKind.TEXT, ADMIN, -100123, "-100123/71", text=f"{command} hello")
    for instance in (listed, other, unconfigured):
        JournalInbound(instance.database).record_batch("telegram", [original], 2, NOW)
        asyncio.run(instance.commands.handle(original))
    assert listed.outbox.pending()
    assert not other.outbox.pending()
    assert not unconfigured.outbox.pending()


def test_an_admin_answer_with_a_secret_is_withheld_and_never_kept(tmp_path: Path) -> None:
    secret = "ghp_" + "a" * 36

    class Leaking(Expert):
        async def turn(self, request: ExpertRequest) -> ExpertResult:
            self.requests.append(request)
            answer = AdminAnswer(
                reply="answer", title="t", tasks=[], findings="f",
                command=f"echo {secret}" if len(self.requests) == 1 else None, reason="r",
            )  # fmt: skip
            return ExpertResult(answer, ExpertSession(0, "fake", "session", "f"), None)

    h = Harness(tmp_path, admins=frozenset({ADMIN}), expert=Leaking())
    _admin_text(h, "/admin hello", ADMIN)
    assert not any(secret in text for text in _sent_texts(h))
    dump = "\n".join(h.database.connection.iterdump())
    assert secret not in dump
    turn = h.log.turn(1)
    assert turn is not None
    assert turn.answer is not None
    assert stored_answer(turn.answer).reply != "answer"


@pytest.mark.parametrize("field", ["command", "reason", "findings", "session"])
def test_loaded_secret_is_not_stored_or_given_to_another_provider(
    tmp_path: Path, field: str
) -> None:
    secret = "synthetic-unusual-value"  # noqa: S105 -- a made-up value

    class Rung:
        def __init__(self, provider: str) -> None:
            self.provider = self.name = provider
            self.calls: list[tuple[ExpertRequest, str | None]] = []
            self.down = False

        async def probe(self) -> None:
            pass

        async def turn(
            self, request: ExpertRequest, *, session_id: str | None, handoff: str | None
        ) -> ExpertResult:
            del session_id
            self.calls.append((request, handoff))
            if self.down:
                raise ExpertError(_RETRY_LATER)
            values = {"command": "ls", "reason": "look", "findings": "facts"}
            if len(self.calls) == 2 and field != "session":
                values[field] = secret
            answer = AdminAnswer(reply="answer", title="title", tasks=[], **values)
            findings = secret if len(self.calls) == 2 and field == "session" else answer.findings
            return ExpertResult(answer, ExpertSession(0, self.provider, "session", findings), None)

    first, second = Rung("first"), Rung("second")
    ladder = LadderExpert(
        [first, second],
        Clock(),
        cooldown=timedelta(minutes=1),
        max_parallel=1,
        handoff_turns=2,
        timeout_s=10,
    )
    h = Harness(
        tmp_path, admins=frozenset({ADMIN}), expert=cast("Expert", ladder), known_secrets=(secret,)
    )
    _admin_text(h, "/admin hello", ADMIN)
    conversation = h.log.get(1)
    assert conversation is not None
    assert conversation.room is not None
    assert conversation.session is not None
    asyncio.run(
        h.turns.handle(
            Inbound(
                2, InboundKind.TEXT, ADMIN, -100123, "2", text="next", thread=int(conversation.room)
            )
        )
    )
    conversation = h.log.get(1)
    assert conversation is not None
    assert first.calls[-1][0].session is None
    assert secret not in "\n".join(h.database.connection.iterdump())
    assert not any(secret in text for text in _sent_texts(h))
    first.down = True
    assert conversation.room is not None
    asyncio.run(
        h.turns.handle(
            Inbound(
                3,
                InboundKind.TEXT,
                ADMIN,
                -100123,
                "3",
                text="continue",
                thread=int(conversation.room),
            )
        )
    )
    request, handoff = second.calls[0]
    assert secret not in repr(request)
    assert secret not in (handoff or "")
