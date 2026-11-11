"""Expert turns and topic commands with the real Expert CLI adapter and a fake command runner."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.expert_cli import ExpertCli
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.expert import Ask
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_ladder import LadderExpert
from shikiri_yaku.app.expert_runs import ConversationRuns
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.ports import CommandResult, ShikiruLinkDownError
from tests.support.destinations import FixedDestinations
from tests.support.expert_proposals import expert_proposals

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.replies import Replies
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.ports import EscalationView, Expert, ShikiruClient, ShikiruTask

OWNER: Final = 1001
STRANGER: Final = 2002
CHAT: Final = -100123
GROUP_ID: Final = -100123
THREAD: Final = 22
CHANNEL: Final = "telegram"
ANSWER: Final[dict[str, object]] = {
    "reply": "Ready",
    "title": "Idea",
    "tasks": [],
    "findings": "Useful facts",
}
MESSAGES: Final = ("question one", "question two", "question three")


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        del seconds


class Reporter:
    def report(self, line: str) -> None:
        raise AssertionError(line)


class Client:
    async def list_tasks(self, statuses: object = None) -> list[ShikiruTask]:
        del statuses
        return []

    async def list_escalations(self) -> list[EscalationView]:
        return []


class Topics:
    def __init__(self) -> None:
        self.created = 0
        self.deleted: list[int] = []

    async def create_expert(self, name: str) -> int:
        del name
        self.created += 1
        return THREAD

    async def rename(self, thread_id: int, name: str) -> None:
        del thread_id, name

    async def delete(self, thread_id: int) -> None:
        self.deleted.append(thread_id)

    async def addressed(self, word: str) -> bool:
        del word
        return False

    def link(self, thread_id: int) -> str:
        return f"https://t.me/c/123/{thread_id}"


class FlakyClient(Client):
    """Fails its first call, as a Shikiru link that is down for a moment does."""

    def __init__(self) -> None:
        self.failures = 1

    async def list_tasks(self, statuses: object = None) -> list[ShikiruTask]:
        if self.failures:
            self.failures -= 1
            message = "down"
            raise ShikiruLinkDownError(message)
        return await super().list_tasks(statuses)


class Runner:
    """Answers as the Claude CLI does; with `hold`, the first call waits until `release` is set."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.calls: list[list[str]] = []
        self.stdin: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        self.calls.append(list(argv))
        self.stdin.append(stdin or "")
        if self.hold and len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        output: dict[str, object] = {
            "is_error": False,
            "session_id": "session",
            "structured_output": ANSWER,
        }
        return CommandResult(0, json.dumps(output), "")


def ladder(runner: Runner) -> LadderExpert:
    rung = ExpertCli("claude", "model", runner, effort=None, timeout_s=20)
    return LadderExpert(
        [rung],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=2,
        handoff_turns=3,
        timeout_s=20,
    )


class Harness:
    def __init__(self, path: Path, expert: Expert, client: Client | None = None) -> None:
        self.database = Database.open(":memory:")
        self.log = Conversations(self.database)
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, Reporter()))
        self.topics = Topics()
        scope = {name: path / name for name in ("api", "web")}
        shared = client or Client()
        clients = {name: cast("ShikiruClient", shared) for name in scope}
        self.runs = ConversationRuns()
        self.ending = ExpertEnding(
            self.log,
            self.runs,
            self.topics,
            self.outbox,
            Clock(),
            directory=path,
            channel=CHANNEL,
            group_id=GROUP_ID,
            retention=timedelta(days=14),
        )
        self.turns = ExpertTurns(
            self.log,
            self.outbox,
            Clock(),
            expert=expert,
            topics=self.topics,
            clients=clients,
            scope=scope,
            directory=path,
            channel=CHANNEL,
            group_id=GROUP_ID,
            runs=self.runs,
            ending=self.ending,
            proposals=expert_proposals(
                self.database,
                self.log,
                self.outbox,
                Clock(),
                clients,
                channel=CHANNEL,
                group_id=GROUP_ID,
            ),
        )
        self.ask = Ask(
            self.log,
            self.outbox,
            Clock(),
            configured=True,
            topics=self.topics,
            turns=self.turns,
            scope=scope,
            channel=CHANNEL,
        )
        self.commands = Commands(
            self.outbox,
            Clock(),
            cast("Status", None),
            cast("OpenCommand", None),
            cast("GoalCommand", None),
            destinations=FixedDestinations(topics={77: "api"}),
            channel=CHANNEL,
            name_max_chars=40,
            ask=self.ask,
        )


@pytest.fixture
def build(tmp_path: Path) -> Generator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(expert: Expert, client: Client | None = None) -> Harness:
        harness = Harness(tmp_path, expert, client)
        made.append(harness)
        return harness

    yield make
    for harness in made:
        harness.database.close()


def _item(
    update_id: int,
    text: str,
    *,
    thread: int | None = None,
    sender: int = OWNER,
    chat: int = CHAT,
) -> Inbound:
    return Inbound(
        update_id, InboundKind.TEXT, sender, chat, str(update_id), text=text, thread=thread
    )


class _Updates:
    def __init__(self, batch: Batch) -> None:
        self.batch = batch

    async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002 -- the port's shape
        return self.batch


class _Poller:
    async def poll_once(self) -> None:
        return None


class _Health:
    def source_recovered(self, scope: str) -> None:
        del scope


class _Log(JournalInbound):
    """Reports when the expected number of items is marked handled."""

    def __init__(self, database: Database, expected: int) -> None:
        super().__init__(database)
        self.expected = expected
        self.count = 0
        self.handled = asyncio.Event()

    def mark_handled(self, update_id: int) -> None:
        super().mark_handled(update_id)
        self.count += 1
        if self.count == self.expected:
            self.handled.set()


def _answer_keys(h: Harness) -> list[str]:
    rows = h.database.connection.execute(
        "SELECT notice_key FROM messages WHERE purpose = 'expert' ORDER BY id"
    ).fetchall()
    return [row["notice_key"] for row in rows]


def _assert_prompts_in_order(runner: Runner, messages: Sequence[str]) -> None:
    assert len(runner.stdin) == len(messages)
    for index, message in enumerate(messages):
        assert message in runner.stdin[index]
        assert not any(later in runner.stdin[index] for later in messages[index + 1 :])


def test_messages_during_a_running_turn_are_answered_in_order_once(
    build: Callable[..., Harness],
) -> None:
    runner = Runner(hold=True)
    h = build(ladder(runner))

    async def scenario() -> None:
        first = asyncio.create_task(h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None))
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        second = asyncio.create_task(h.turns.handle(_item(2, MESSAGES[1], thread=THREAD)))
        third = asyncio.create_task(h.turns.handle(_item(3, MESSAGES[2], thread=THREAD)))
        runner.release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), timeout=1)
        await h.turns.handle(_item(2, MESSAGES[1], thread=THREAD))

    asyncio.run(scenario())

    _assert_prompts_in_order(runner, MESSAGES)
    assert _answer_keys(h) == ["ask:answer:1", "ask:answer:2", "ask:answer:3"]


def test_a_reply_in_an_expert_topic_is_a_turn(build: Callable[..., Harness]) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None))
    reply = Inbound(
        2,
        InboundKind.REPLY,
        OWNER,
        CHAT,
        "2",
        reply_to_ref="1",
        text=MESSAGES[1],
        thread=THREAD,
    )

    async def scenario() -> None:
        log = _Log(h.database, expected=1)
        log.record_batch(CHANNEL, [reply], 3, Clock().now())
        service = InboundService(
            cast("InboundPoller", _Poller()),
            log,
            cast("Replies", None),
            cast("HealthSource", _Health()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await service.tick()
        await asyncio.wait_for(log.handled.wait(), timeout=1)

    asyncio.run(scenario())

    _assert_prompts_in_order(runner, MESSAGES[:2])
    assert _answer_keys(h) == ["ask:answer:1", "ask:answer:2"]


def test_a_private_origin_ends_through_the_expert_group_topic(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))

    async def scenario() -> None:
        ask_log = _Log(h.database, expected=1)
        ask_log.record_batch(
            CHANNEL, [_item(1, f"/ask {MESSAGES[0]}", chat=OWNER)], 2, Clock().now()
        )
        ask_service = InboundService(
            cast("InboundPoller", _Poller()),
            ask_log,
            cast("Replies", h.commands),
            cast("HealthSource", _Health()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await ask_service.tick()
        await asyncio.wait_for(ask_log.handled.wait(), timeout=1)

        conversation = h.log.get(1)
        assert conversation is not None
        assert conversation.chat == OWNER
        assert not h.turns.owns(_item(2, "/close", thread=THREAD, chat=OWNER))

        close_log = _Log(h.database, expected=1)
        closed = Inbound(2, InboundKind.TOPIC_CLOSED, OWNER, GROUP_ID, "2", thread=THREAD)
        close_log.record_batch(CHANNEL, [closed], 3, Clock().now())
        close_service = InboundService(
            cast("InboundPoller", _Poller()),
            close_log,
            cast("Replies", h.commands),
            cast("HealthSource", _Health()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await close_service.tick()
        await asyncio.wait_for(close_log.handled.wait(), timeout=1)

    asyncio.run(scenario())

    assert h.log.get(1) is None
    assert h.topics.deleted == [THREAD]


def test_a_later_message_waits_for_the_retry_of_a_turn_whose_context_failed(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner), FlakyClient())

    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError):
            await h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None)
        await h.turns.handle(_item(2, MESSAGES[1], thread=THREAD))
        await h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None)

    asyncio.run(scenario())

    _assert_prompts_in_order(runner, MESSAGES[:2])
    assert _answer_keys(h) == ["ask:answer:1", "ask:answer:2"]


def test_second_turn_resumes_the_session_and_takes_the_effort(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))

    async def scenario() -> None:
        await h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None)
        await h.turns.handle(_item(2, "/effort high", thread=THREAD))
        await h.turns.handle(_item(3, MESSAGES[1], thread=THREAD))

    asyncio.run(scenario())

    argv = runner.calls[-1]
    assert argv[argv.index("--resume") + 1] == "session"
    assert argv[argv.index("--effort") + 1] == "high"
    [effort_reply] = [
        pending for pending in h.outbox.pending() if pending.message.text.startswith("Effort set")
    ]
    assert effort_reply.conversation_id == 1


def test_a_non_owner_in_an_expert_topic_starts_nothing(build: Callable[..., Harness]) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None))
    journal = JournalInbound(h.database)
    stranger = _item(2, MESSAGES[1], thread=THREAD, sender=STRANGER)
    poller = InboundPoller(
        _Updates(Batch(2, (stranger,))),
        journal,
        Clock(),
        channel=CHANNEL,
        owners=[OWNER],
        wait_s=20,
    )

    asyncio.run(poller.poll_once())

    assert journal.pending() == []
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


def test_expert_topic_answers_other_commands_with_its_own_help(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/ask {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, "/status", thread=THREAD)))

    assert h.outbox.command_reply_queued("expert:command:2")
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


@pytest.mark.parametrize("text", ["/effort high", "/summarize"])
@pytest.mark.parametrize("thread", [None, 77])
def test_expert_commands_are_unavailable_outside_expert_topics(
    build: Callable[..., Harness], text: str, thread: int | None
) -> None:
    runner = Runner()
    h = build(ladder(runner))

    asyncio.run(h.commands.handle(_item(2, text, thread=thread)))

    [reply] = [
        pending for pending in h.outbox.pending() if "Unknown command" in pending.message.text
    ]
    assert reply.message.text.startswith("Unknown command")
    assert h.log.get(2) is None
    assert not runner.calls
