"""Expert conversation opening and crash recovery without Telegram or agents."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.expert import Ask
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_runs import ConversationRuns, conversation_directory
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertResult, ExpertSession
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import (
    TOPIC_GONE_REASON,
    ChannelError,
    ChannelRefusedError,
    ExpertError,
    ShikiruTask,
)
from tests.support.destinations import FixedDestinations
from tests.support.expert_proposals import expert_proposals

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
    from shikiri_yaku.ports import EscalationView, Message, ShikiruClient, Updates as UpdatesPort

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


class Topics:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.renamed: list[str] = []
        self.refuse_rename = False

    async def delete(self, thread_id: int) -> None:
        raise NotImplementedError

    async def create_expert(self, name: str) -> int:
        self.created.append(name)
        return 21 + len(self.created)

    async def rename(self, thread_id: int, name: str) -> None:  # noqa: ARG002
        self.renamed.append(name)
        if self.refuse_rename:
            message = "refused"
            raise ChannelRefusedError(message)

    async def addressed(self, word: str) -> bool:
        return word.lower() == "/ask@bot"

    async def bot_name(self) -> str:
        return "bot"

    def link(self, thread_id: int) -> str:
        return f"https://t.me/c/123/{thread_id}"


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
            raise ChannelRefusedError(TOPIC_GONE_REASON)
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
            directory=path,
            channel="telegram",
            group_id=-100123,
            retention=timedelta(days=14),
        )
        clients = clients or {name: cast("ShikiruClient", Client()) for name in ("api", "web")}
        self.turns = ExpertTurns(
            self.log,
            self.outbox,
            Clock(),
            expert=self.expert,
            topics=self.topics,
            clients=clients,
            scope={name: path / name for name in ("api", "web")},
            directory=path,
            channel="telegram",
            group_id=-100123,
            runs=self.runs,
            ending=self.ending,
            proposals=expert_proposals(
                self.database,
                self.log,
                self.outbox,
                Clock(),
                clients,
                channel="telegram",
                group_id=-100123,
            ),
        )
        self.ask = Ask(
            self.log,
            self.outbox,
            Clock(),
            configured=enabled,
            topics=self.topics if group else None,
            turns=self.turns,
            scope={name: path / name for name in ("api", "web")},
            channel="telegram",
            final=JournalFinalEdits(self.database, cast("JournalReplies", None)),
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
            shared_group=shared,
            ask=self.ask,
        )

    def send(self, text: str = "/ask hello", thread: int | None = None) -> None:
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
        (False, True, "/ask hello", False, None, 1),
        (True, False, "/ask hello", False, None, 1),
        (True, True, "/ask", False, None, 1),
        (True, True, "/ask hello", True, None, 1),
        (True, True, "/ask@bot hello", True, None, 2),
        (True, True, "/ask@another hello", True, None, 0),
        (True, True, "/ask hello", True, 77, 2),
        (True, True, "/ask hello", True, 78, 0),
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
    if replies == 1 and shared and text == "/ask hello" and thread is None:
        assert pending[0].message.reply_to == "1"
        assert pending[0].message.buttons[0].label == "/ask@bot"
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
    h.log.opened(1, 22)
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
            Inbound(update, InboundKind.TEXT, 5, -100123, str(update), text="/ask hello")
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
        "SELECT purpose, conversation_id FROM messages WHERE notice_key = 'ask:answer:1'"
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
                text="/ask hello",
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
    assert remaining.thread_id == 22
    assert conversation_directory(tmp_path, remaining.id).exists()


def test_private_origin_interrupted_turn_is_reported_in_the_group_topic(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.log.create(1, chat=5, thread=None, text="hello", scope=["api"], now=NOW)
    h.log.opened(1, 22)
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
        update_id, InboundKind.TEXT, 5, -100123, message_ref, text=f"/ask question {update_id}"
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
        assert conversation.thread_id is not None
        assert button.message.buttons[0].url == h.topics.link(conversation.thread_id)


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
    [(False, "/ask hello"), (True, "/ask@bot hello"), (True, "/ask hello")],
)
def test_ask_in_general_through_the_inbound_service(
    tmp_path: Path,
    shared: bool,  # noqa: FBT001 -- table column
    text: str,
) -> None:
    h = Harness(tmp_path, shared=shared)
    _pump(h, _general_payload(text))

    opened = text == "/ask@bot hello" or not shared
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
    assert offer.message.buttons[0].label == "/ask@bot"
    channel = Channel()
    asyncio.run(OutboxSender(h.outbox, channel, Clock(), interval_s=1).tick())
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    forged = Inbound(3, InboundKind.TAP, 5, -100123, "-100123/999", data="expert:1")
    assert not asyncio.run(h.commands.tap(forged))
    assert asyncio.run(h.commands.tap(tap))
    assert asyncio.run(h.commands.tap(tap))
    assert len(h.topics.created) == len(h.expert.requests) == 1
    final = JournalFinalEdits(h.database, cast("JournalReplies", None))
    [edit] = final.to_edit()
    assert edit.message_ref == tap.message_ref
    assert edit.message.buttons[0].url == "https://t.me/c/123/22"


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
    h.log.opened(1, 22)
    h.turns.recover()
    tap = Inbound(2, InboundKind.TAP, 5, -100123, "-100123/1", data="expert:1")
    assert asyncio.run(h.commands.tap(tap))
    assert not h.topics.created
    assert len(h.expert.requests) == 1
    final = JournalFinalEdits(h.database, cast("JournalReplies", None))
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

    class Updates:
        async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002
            return Batch(2, (tap,))

    poller = InboundPoller(
        cast("UpdatesPort", Updates()), inbound, Clock(), channel="telegram", owners=[5], wait_s=1
    )
    asyncio.run(poller.poll_once())
    assert all(item.update_id != 2 for item in inbound.pending())
    assert not h.topics.created


def test_unconfigured_expert_offers_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, enabled=False, shared=True)
    h.send()
    assert not h.outbox.pending()
    assert not h.topics.created


def test_selection_turns_do_not_block_inbound_or_each_other(tmp_path: Path) -> None:
    async def scenario() -> None:
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
