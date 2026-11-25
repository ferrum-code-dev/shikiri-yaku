"""Expert turns and topic commands with the real Expert CLI adapter and a fake command runner."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.agents.claude import ClaudeExpert
from shikiri_yaku.adapters.agents.muse import MuseExpert
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.expert import Ask
from shikiri_yaku.app.expert_admin import AdminAccess
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_ladder import LadderExpert
from shikiri_yaku.app.expert_runs import ConversationRuns
from shikiri_yaku.app.expert_sketches import ExpertSketches
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.expert_web import ExpertWebRequests
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.domain.inbound import Batch, FileRef, Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.journal.web import JournalWeb
from shikiri_yaku.ports import (
    DOWNLOAD_LIMIT_BYTES,
    TOPIC_GONE_REASON,
    ChannelRefusedError,
    ChannelUnavailableError,
    CommandResult,
    ExpertError,
    ShikiruLinkDownError,
)
from tests.support.attachments import PNG, FakeDownloads, FakeSketcher
from tests.support.copies import Platform
from tests.support.copies import deliver as deliver_copies
from tests.support.destinations import FixedDestinations
from tests.support.expert_proposals import expert_proposals
from tests.support.secrets import SECRETS

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.expert import ExpertRequest, ExpertResult
    from shikiri_yaku.ports import (
        EscalationView,
        Expert,
        ReplyLog,
        ShikiruClient,
        ShikiruTask,
        Sketcher,
    )

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
STORED: Final = json.dumps({**ANSWER, "reply": "Stored reply"})
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
        if thread_id in self.deleted:
            raise ChannelRefusedError(TOPIC_GONE_REASON)
        self.deleted.append(thread_id)

    async def addressed(self, word: str) -> bool:
        del word
        return False

    async def bot_name(self) -> str:
        return "bot"

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

    def __init__(self, *, hold: bool = False, answer: dict[str, object] | None = None) -> None:
        self.hold = hold
        self.answer = ANSWER if answer is None else answer
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
        if argv[0] == "muse":
            session = argv[argv.index("--session-id") + 1]
            muse_output = {
                "stream": {"kind": "session", "id": session},
                "payload_type": "run.terminal.completed",
                "payload": {"text": json.dumps(ANSWER)},
            }
            return CommandResult(0, json.dumps(muse_output), "")
        output: dict[str, object] = {
            "is_error": False,
            "session_id": "session",
            "structured_output": self.answer,
        }
        return CommandResult(0, json.dumps(output), "")


def ladder(runner: Runner, *, muse: bool = False) -> LadderExpert:
    expert_class = MuseExpert if muse else ClaudeExpert
    rung = expert_class("model", runner, effort=None, timeout_s=20)
    return LadderExpert(
        [rung],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=2,
        handoff_turns=3,
        timeout_s=20,
    )


class Harness:
    def __init__(
        self,
        path: Path,
        expert: Expert,
        client: Client | None = None,
        *,
        muse: bool = False,
        admins: frozenset[int] = frozenset(),
        shared_group: bool = False,
        downloads: FakeDownloads | None = None,
        sketcher: Sketcher | None = None,
    ) -> None:
        self.database = Database.open(":memory:")
        self.log = Conversations(self.database)
        self.outbox = JournalOutbox(self.database, MessageStore(self.database, Reporter()))
        self.topics = Topics()
        scope_root = Path("/component") if muse else path
        self.downloads = FakeDownloads() if downloads is None else downloads
        scope = {name: scope_root / name for name in ("api", "web")}
        shared = client or Client()
        clients = {name: cast("ShikiruClient", shared) for name in scope}
        self.runs = ConversationRuns()
        replies = JournalReplies(
            self.database, SignalStore(self.database), OpStore(self.database), channel_name=CHANNEL
        )
        self.final = JournalFinalEdits(self.database, replies)
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
            admin_idle=timedelta(minutes=15),
            final=self.final,
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
            downloads=self.downloads,
            sketches=ExpertSketches(
                sketcher, self.outbox, Clock(), directory=path, channel=CHANNEL, group_id=GROUP_ID
            ),
            web=ExpertWebRequests(
                JournalWeb(self.database),
                self.outbox,
                Clock(),
                channel=CHANNEL,
                group_id=GROUP_ID,
                access=AdminAccess(self.log, Clock(), admins=frozenset()),
            ),
            proposals=expert_proposals(
                self.database,
                self.log,
                self.outbox,
                Clock(),
                clients,
                channel=CHANNEL,
                group_id=GROUP_ID,
                access=AdminAccess(self.log, Clock(), admins=admins),
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
            shared_group=shared_group,
            ask=self.ask,
        )


@pytest.fixture
def build(tmp_path: Path) -> Generator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(
        expert: Expert,
        client: Client | None = None,
        *,
        muse: bool = False,
        admins: frozenset[int] = frozenset(),
        shared_group: bool = False,
        downloads: FakeDownloads | None = None,
        sketcher: Sketcher | None = None,
    ) -> Harness:
        harness = Harness(
            tmp_path,
            expert,
            client,
            muse=muse,
            admins=admins,
            shared_group=shared_group,
            downloads=downloads,
            sketcher=sketcher,
        )
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
        first = asyncio.create_task(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        second = asyncio.create_task(h.turns.handle(_item(2, MESSAGES[1], thread=THREAD)))
        third = asyncio.create_task(h.turns.handle(_item(3, MESSAGES[2], thread=THREAD)))
        runner.release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), timeout=1)
        await h.turns.handle(_item(2, MESSAGES[1], thread=THREAD))

    asyncio.run(scenario())

    _assert_prompts_in_order(runner, MESSAGES)
    assert _answer_keys(h) == ["expert:answer:1", "expert:answer:2", "expert:answer:3"]


_MIXED_RUNG_FAILURES: Final = [
    ("claude", "login expired; token refused"),
    ("muse", "rate limit; retry after reset"),
    ("codex", "schema rejected"),
    ("claude-backup", "login expired"),
    ("muse-backup", "quota exhausted"),
]
_LONG_LADDER_FAILURES: Final = [
    (
        f"claude:some-long-model-name-v{index}",
        f"claude:some-long-model-name-v{index}: login expired",
    )
    for index in range(20)
]
_HUGE_LADDER_FAILURES: Final = [
    (f"muse:model-{index}", f"muse:model-{index}: quota exhausted") for index in range(1000)
]


class _FailedRung:
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.provider = name
        self.reason = reason

    async def turn(
        self,
        request: ExpertRequest,
        *,
        session_id: str | None,
        handoff: str | None,
    ) -> ExpertResult:
        del request, session_id, handoff
        raise ExpertError(self.reason)

    async def probe(self) -> None:
        pass


class _HangingRung(_FailedRung):
    async def turn(
        self,
        request: ExpertRequest,
        *,
        session_id: str | None,
        handoff: str | None,
    ) -> ExpertResult:
        del request, session_id, handoff
        await asyncio.Event().wait()
        raise ExpertError(self.reason)


def _expert_topic_message(build: Callable[..., Harness], expert: LadderExpert) -> str:
    h = build(expert)
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    row = h.database.connection.execute(
        "SELECT body FROM messages WHERE notice_key = 'expert:answer:1'"
    ).fetchone()
    return json.loads(row["body"])["text"]


def _ladder(rungs: list[_FailedRung], *, timeout_s: float = 20) -> LadderExpert:
    return LadderExpert(
        rungs,
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=1,
        handoff_turns=3,
        timeout_s=timeout_s,
    )


def test_each_failed_rung_is_named_with_its_reason_in_the_topic(
    build: Callable[..., Harness],
) -> None:
    message = _expert_topic_message(
        build,
        _ladder([_FailedRung(name, reason) for name, reason in _MIXED_RUNG_FAILURES]),
    )

    assert message == (
        "No Expert agent could answer.\n"
        "claude: login expired; token refused\n"
        "muse: rate limit; retry after reset\n"
        "codex: schema rejected\n"
        "claude-backup: login expired\n"
        "muse-backup: quota exhausted"
    )


def test_a_timed_out_rung_is_named_with_the_timeout_in_the_topic(
    build: Callable[..., Harness],
) -> None:
    message = _expert_topic_message(
        build,
        _ladder([_HangingRung("claude", "unused")], timeout_s=0.01),
    )

    assert message == "No Expert agent could answer.\nclaude: timed out after 0.01s"


@pytest.mark.parametrize(
    ("rung_failures", "first_line"),
    [
        (_LONG_LADDER_FAILURES, "claude:some-long-model-name-v0: login expired"),
        (_HUGE_LADDER_FAILURES, "muse:model-0: quota exhausted"),
    ],
    ids=["long-ladder", "huge-ladder"],
)
def test_a_long_ladder_shows_its_first_reasons_and_counts_the_rest(
    build: Callable[..., Harness],
    rung_failures: list[tuple[str, str]],
    first_line: str,
) -> None:
    message = _expert_topic_message(
        build,
        _ladder([_FailedRung(name, reason) for name, reason in rung_failures]),
    )

    assert len(message) <= 1000
    lines = message.splitlines()
    omitted = re.search(r"\+(\d+) more rungs not shown", message)
    shown = lines[1:-1] if omitted else lines[1:]
    assert len(shown) + (int(omitted.group(1)) if omitted else 0) == len(rung_failures)
    assert shown[0] == first_line
    assert all(len(line) <= 240 for line in shown)
    for line, (name, reason) in zip(shown, rung_failures, strict=False):
        assert f"{name}: {reason.removeprefix(name + ': ')}".startswith(line)


def test_a_reply_in_an_expert_topic_is_a_turn(build: Callable[..., Harness]) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
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
    assert _answer_keys(h) == ["expert:answer:1", "expert:answer:2"]


def test_a_private_origin_ends_through_the_expert_group_topic(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))

    async def scenario() -> None:
        ask_log = _Log(h.database, expected=1)
        ask_log.record_batch(
            CHANNEL, [_item(1, f"/expert {MESSAGES[0]}", chat=OWNER)], 2, Clock().now()
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
    deliver_copies(h.outbox, h.final, Clock(), Platform(), gone=h.ending)
    asyncio.run(h.ending.sweep())

    assert h.log.get(1) is None
    assert h.topics.deleted == [THREAD]


def test_a_later_message_waits_for_the_retry_of_a_turn_whose_context_failed(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner), FlakyClient())

    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError):
            await h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None)
        await h.turns.handle(_item(2, MESSAGES[1], thread=THREAD))
        await h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None)

    asyncio.run(scenario())

    _assert_prompts_in_order(runner, MESSAGES[:2])
    assert _answer_keys(h) == ["expert:answer:1", "expert:answer:2"]


def test_second_turn_resumes_the_session_and_takes_the_effort(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))

    async def scenario() -> None:
        await h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None)
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
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
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
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, "/status", thread=THREAD)))

    assert h.outbox.command_reply_queued("expert:command:2")
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "/expert hello",
        "/admin hello",
        "/submit all",
        "/status",
        "/pending",
        "/submit api Add the export",
        "/unknowncmd",
    ],
)
def test_other_commands_in_expert_topic_get_expert_help_and_start_no_turn(
    build: Callable[..., Harness], text: str
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, text, thread=THREAD)))

    assert h.outbox.command_reply_queued("expert:command:2")
    pending = h.outbox.pending()
    help_reply = next(p for p in pending if "/effort" in p.message.text)
    assert "/close" in help_reply.message.text
    assert "/expert" not in help_reply.message.text
    assert "/admin" not in help_reply.message.text
    assert "/status" not in help_reply.message.text
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


def test_expert_topic_close_ends_conversation_and_starts_no_turn(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, "/close", thread=THREAD)))

    assert h.log.turn(2) is None
    assert len(runner.calls) == 1
    from tests.support.copies import Platform, deliver  # noqa: PLC0415

    deliver(h.outbox, h.final, Clock(), Platform(), gone=h.ending)
    asyncio.run(h.ending.sweep())
    assert h.log.get(1) is None


def test_expert_topic_help_lists_only_expert_commands(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, "/help", thread=THREAD)))

    help_reply = next(p for p in h.outbox.pending() if "/effort" in p.message.text)
    assert "/summarize" in help_reply.message.text
    assert "/close" in help_reply.message.text
    assert "/help" in help_reply.message.text
    assert "/expert" not in help_reply.message.text
    assert "/admin" not in help_reply.message.text
    assert "/status" not in help_reply.message.text
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


def test_expert_commands_have_leading_space_tolerance(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    asyncio.run(h.turns.handle(_item(2, "  /help", thread=THREAD)))

    assert h.outbox.command_reply_queued("expert:command:2")
    assert h.log.turn(2) is None
    assert len(runner.calls) == 1


def test_inbound_service_routes_expert_topic_commands_to_expert_and_others_to_commands(
    build: Callable[..., Harness],
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    command_in_expert = _item(2, "/status", thread=THREAD)
    command_outside = _item(3, "/help", thread=77)

    assert h.turns.owns(command_in_expert)
    assert not h.turns.owns(command_outside)

    async def scenario() -> None:
        log = _Log(h.database, expected=2)
        log.record_batch(CHANNEL, [command_in_expert, command_outside], 4, Clock().now())
        replies = Replies(
            JournalReplies(
                h.database, SignalStore(h.database), OpStore(h.database), channel_name=CHANNEL
            ),
            h.outbox,
            Clock(),
            {},
            channel=CHANNEL,
            commands=h.commands,
            goal=cast("GoalCommand", None),
            expert_turns=h.turns,
        )
        service = InboundService(
            cast("InboundPoller", _Poller()),
            log,
            replies,
            cast("HealthSource", _Health()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await service.tick()
        await asyncio.wait_for(log.handled.wait(), timeout=1)

    asyncio.run(scenario())
    assert h.outbox.command_reply_queued("expert:command:2")
    assert h.outbox.command_reply_queued("command:3")
    assert h.log.turn(2) is None
    assert h.log.turn(3) is None
    assert len(runner.calls) == 1
    pending = h.outbox.pending()
    expert_reply = next(p for p in pending if "/close" in p.message.text)
    assert "/effort" in expert_reply.message.text
    global_reply = next(
        p for p in pending if "/status" in p.message.text and "/close" not in p.message.text
    )
    assert "/submit" in global_reply.message.text


@pytest.mark.parametrize("text", ["hello", "/help"])
def test_shared_group_inbound_routing_in_foreign_expert_topic(
    build: Callable[..., Harness], text: str
) -> None:
    owner_runner = Runner()
    other_runner = Runner()
    owner = build(ladder(owner_runner), shared_group=True)
    other = build(ladder(other_runner), shared_group=True)
    asyncio.run(owner.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    item = _item(10, text, thread=THREAD)

    assert owner.turns.owns(item)
    assert not other.turns.owns(item)

    async def route(h: Harness) -> None:
        log = _Log(h.database, expected=1)
        log.record_batch(CHANNEL, [item], 11, Clock().now())
        replies = Replies(
            JournalReplies(
                h.database, SignalStore(h.database), OpStore(h.database), channel_name=CHANNEL
            ),
            h.outbox,
            Clock(),
            {},
            channel=CHANNEL,
            commands=h.commands,
            goal=cast("GoalCommand", None),
            expert_turns=h.turns,
        )
        service = InboundService(
            cast("InboundPoller", _Poller()),
            log,
            replies,
            cast("HealthSource", _Health()),
            Clock(),
            interval_s=1,
            expert=h.turns,
        )
        await service.tick()
        await asyncio.wait_for(log.handled.wait(), timeout=1)

    asyncio.run(route(owner))
    asyncio.run(route(other))

    assert len(other_runner.calls) == 0
    assert other.log.turn(10) is None
    assert other.outbox.pending() == []
    assert not other.outbox.command_reply_queued("command:10")
    assert not other.outbox.command_reply_queued("expert:command:10")
    if text == "/help":
        assert owner.outbox.command_reply_queued("expert:command:10")
        assert owner.log.turn(10) is None
        assert len(owner_runner.calls) == 1
    else:
        assert owner.log.turn(10) is not None
        assert len(owner_runner.calls) == 2


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


@pytest.mark.parametrize("text", ["/close", "/effort", "/effort high", "/summarize"])
@pytest.mark.parametrize("thread", [None, 77])
def test_close_and_effort_outside_expert_are_unknown(
    build: Callable[..., Harness], text: str, thread: int | None
) -> None:
    runner = Runner()
    h = build(ladder(runner))
    asyncio.run(h.commands.handle(_item(2, text, thread=thread)))
    [reply] = [
        pending for pending in h.outbox.pending() if "Unknown command" in pending.message.text
    ]
    assert reply.message.text.startswith("Unknown command")
    assert "/expert" in reply.message.text
    help_body = reply.message.text.split("\n", 1)[-1]
    assert "/close" not in help_body
    assert not runner.calls
    assert h.log.get(2) is None


@pytest.mark.parametrize("muse", [False, True])
def test_web_decision_does_not_block_inbound_and_two_taps_run_once(
    build: Callable[..., Harness],
    muse: bool,  # noqa: FBT001
) -> None:
    runner = Runner(hold=True)
    h = build(ladder(runner, muse=muse), muse=muse)
    now = Clock().now()
    h.log.create(1, chat=CHAT, thread=None, text="question", scope=["api"], now=now)
    conversation = h.log.get(1)
    assert conversation is not None
    h.log.opened(1, THREAD)
    h.log.start(1)
    h.log.finish(1, json.dumps(ANSWER), None, None, now)
    web = JournalWeb(h.database)
    web.request(1, "docs.example")
    h.database.connection.execute(
        "INSERT INTO messages (direction, channel, chat, platform_message_id, "
        "notice_key, delivery, created_at) VALUES ('out', ?, ?, '9', 'expert:web:1', 'sent', ?)",
        (CHANNEL, str(CHAT), now.isoformat()),
    )
    first = Inbound(2, InboundKind.TAP, OWNER, CHAT, "9", data="w:1", thread=THREAD)
    second = Inbound(3, InboundKind.TAP, OWNER, CHAT, "9", data="w:1", thread=THREAD)
    command = _item(4, "/help")
    log = _Log(h.database, expected=3)
    log.record_batch(CHANNEL, [first, second, command], 5, now)
    replies = Replies(
        JournalReplies(
            h.database, SignalStore(h.database), OpStore(h.database), channel_name=CHANNEL
        ),
        h.outbox,
        Clock(),
        {},
        channel=CHANNEL,
        commands=h.commands,
        goal=cast("GoalCommand", None),
        expert_turns=h.turns,
    )
    service = InboundService(
        cast("InboundPoller", _Poller()),
        log,
        replies,
        cast("HealthSource", _Health()),
        Clock(),
        interval_s=1,
        expert=h.turns,
    )

    async def scenario() -> None:
        await asyncio.wait_for(service.tick(), timeout=1)
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        assert h.outbox.command_reply_queued("command:4")
        assert h.log.turn(2) is not None
        assert not runner.release.is_set()
        await asyncio.wait_for(service.tick(), timeout=1)
        assert [item.update_id for item in log.pending()] == [2]
        runner.release.set()
        await asyncio.wait_for(log.handled.wait(), timeout=1)
        await service.tick()
        await replies.handle(first)

    asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert h.log.turn(3) is None
    assert web.granted("docs.example")
    assert log.pending() == []


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("echo bad\x1btext", "reason"),
        ("echo ok", "bad\u202ereason"),
        ("echo bad\u034ftext", "reason"),
        ("echo bad\u2800text", "reason"),
        ("echo bad\u115ftext", "reason"),
        ("echo bad\u2007text", "reason"),
        ("echo ok", "bad\u2800reason"),
        ("x" * 4096, "reason"),
    ],
)
def test_admin_command_that_cannot_be_shown_is_not_stored(
    build: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch, command: str, reason: str
) -> None:
    monkeypatch.setitem(ANSWER, "command", command)
    monkeypatch.setitem(ANSWER, "reason", reason)

    class RefusalRunner(Runner):
        async def run(
            self,
            argv: Sequence[str],
            *,
            timeout_s: float,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            stdin: str | None = None,
        ) -> CommandResult:
            result = await super().run(argv, timeout_s=timeout_s, cwd=cwd, env=env, stdin=stdin)
            monkeypatch.setitem(ANSWER, "command", None)
            return result

    runner = RefusalRunner()
    h = build(ladder(runner))
    h.log.create(
        1, chat=CHAT, thread=None, text="question", scope=[], now=Clock().now(), mode="admin"
    )
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None

    asyncio.run(h.turns.run(conversation.id, 1))

    turn = h.log.turn(1)
    assert turn is not None
    assert turn.answer is not None
    assert command not in turn.answer
    assert json.loads(turn.answer)["command"] is None
    assert len(runner.calls) == 2
    assert "was refused" in runner.stdin[1]


def test_consecutive_refused_commands_each_get_an_expert_turn(
    build: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ANSWER, "command", "echo bad\u2800first")
    monkeypatch.setitem(ANSWER, "reason", "reason")

    class RepeatedRefusalRunner(Runner):
        async def run(
            self,
            argv: Sequence[str],
            *,
            timeout_s: float,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            stdin: str | None = None,
        ) -> CommandResult:
            result = await super().run(argv, timeout_s=timeout_s, cwd=cwd, env=env, stdin=stdin)
            monkeypatch.setitem(
                ANSWER, "command", "echo bad\u2800second" if len(self.calls) == 1 else None
            )
            return result

    runner = RepeatedRefusalRunner()
    h = build(ladder(runner))
    h.log.create(
        1, chat=CHAT, thread=None, text="question", scope=[], now=Clock().now(), mode="admin"
    )
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None
    asyncio.run(h.turns.run(conversation.id, 1))
    assert len(runner.calls) == 3
    assert all("was refused" in prompt for prompt in runner.stdin[1:])
    assert h.log.oldest_pending(conversation.id) is None
    dump = "\n".join(h.database.connection.iterdump())
    assert "echo bad" not in dump


def test_refusal_follow_up_keeps_conversation_order_in_history(
    build: Callable[..., Harness],
) -> None:
    h = build(ladder(Runner()))
    now = Clock().now()
    h.log.create(1, chat=CHAT, thread=None, text="one", scope=[], now=now, mode="admin")
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None
    h.log.add_turn(conversation.id, 5, "two", now)
    for update_id in (1, 5):
        h.log.start(update_id)
        h.log.finish(update_id, STORED, None, None, now)
    followup = h.log.refuse_admin(conversation.id, 5, now)
    assert followup is not None
    h.log.start(followup)
    h.log.finish(followup, STORED, None, None, now)
    h.log.add_turn(conversation.id, 9, "three", now)
    h.log.start(9)
    h.log.finish(9, STORED, None, None, now)

    assert [t.update_id for t in h.log.history(conversation.id, before=followup)] == [1, 5]
    assert [t.update_id for t in h.log.history(conversation.id, before=9)] == [1, 5, followup]


def test_done_refusal_follow_up_is_delivered_on_replay(build: Callable[..., Harness]) -> None:
    h = build(ladder(Runner()))
    now = Clock().now()
    h.log.create(1, chat=CHAT, thread=None, text="one", scope=[], now=now, mode="admin")
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None
    h.log.start(1)
    h.log.finish(1, STORED, None, None, now)
    followup = h.log.refuse_admin(conversation.id, 1, now)
    assert followup is not None
    h.log.start(followup)
    h.log.finish(followup, STORED, None, None, now)

    asyncio.run(h.turns.run(conversation.id, 1))

    assert [p.message.text for p in h.outbox.pending()].count("Stored reply") == 2


def test_decline_replay_delivers_finished_turn_once(
    build: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner()
    h = build(ladder(runner), admins=frozenset({OWNER}))
    monkeypatch.setitem(ANSWER, "command", "echo first")
    monkeypatch.setitem(ANSWER, "reason", "first reason")
    h.log.create(
        1, chat=CHAT, thread=None, text="question", scope=[], now=Clock().now(), mode="admin"
    )
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None
    asyncio.run(h.turns.run(conversation.id, 1))
    op = h.database.connection.execute("SELECT id FROM ops WHERE kind = 'admin'").fetchone()[0]
    message = h.database.connection.execute(
        "SELECT id FROM messages WHERE notice_key = ?", (f"admin:proposal:{op}",)
    ).fetchone()
    MessageStore(h.database, Reporter()).confirm_delivery(message[0], "admin-ref")
    tap = Inbound(2, InboundKind.TAP, OWNER, CHAT, "admin-ref", data=f"v:{op}", thread=THREAD)
    monkeypatch.setitem(ANSWER, "reply", "Decline answer")
    monkeypatch.setitem(ANSWER, "command", "echo replacement")
    monkeypatch.setitem(ANSWER, "reason", "replacement reason")

    async def stop_before_delivery(
        _self: ExpertTurns, _conversation_id: int, _update_id: int
    ) -> None:
        message = "stopped before delivery"
        raise RuntimeError(message)

    with monkeypatch.context() as patch:
        patch.setattr(ExpertTurns, "_deliver", stop_before_delivery)
        with pytest.raises(RuntimeError, match="stopped before delivery"):
            asyncio.run(h.turns.web_tap(tap))

    turn = h.log.turn(2)
    assert turn is not None
    assert turn.state == "done"
    assert turn.answer is not None
    assert json.loads(turn.answer)["command"] == "echo replacement"
    assert len(runner.calls) == 2
    assert all(p.message.text != "Decline answer" for p in h.outbox.pending())
    assert (
        h.database.connection.execute("SELECT count(*) FROM ops WHERE kind = 'admin'").fetchone()[0]
        == 1
    )

    assert asyncio.run(h.turns.web_tap(tap))
    assert asyncio.run(h.turns.web_tap(tap))
    assert len(runner.calls) == 2
    messages = [p.message for p in h.outbox.pending()]
    assert sum(message.text == "Decline answer" for message in messages) == 1
    proposals = [message for message in messages if message.text.endswith("echo replacement")]
    assert len(proposals) == 1
    assert [button.label for button in proposals[0].buttons] == ["Run", "Decline"]
    assert (
        h.database.connection.execute("SELECT count(*) FROM ops WHERE kind = 'admin'").fetchone()[0]
        == 2
    )


def test_restart_resumes_pending_admin_output_before_new_turns(
    build: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    from shikiri_yaku.journal.proposals import JournalProposals  # noqa: PLC0415

    monkeypatch.setitem(ANSWER, "command", None)
    monkeypatch.setitem(ANSWER, "reason", "")
    runner = Runner()
    h = build(ladder(runner))
    now = Clock().now()
    h.log.create(1, chat=CHAT, thread=None, text="question", scope=[], now=now, mode="admin")
    h.log.opened(1, THREAD)
    conversation = h.log.get(1)
    assert conversation is not None
    journal = JournalProposals(h.database, OpStore(h.database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(conversation.id, 10, "echo safe", "reason", now)
    assert journal.decide_admin(
        op, 11, OWNER, admin=True, approve=True, now=now, cutoff=now - timedelta(minutes=1)
    )
    assert journal.issue_admin(op, now) is not None
    output_turn = journal.finish_admin(op, now, "safe output", 0, 1.0, cut=False)
    assert output_turn is not None
    h.turns.recover()
    asyncio.run(h.turns.resume_admin_followups())
    asyncio.run(h.turns.resume_admin_followups())
    assert len(runner.calls) == 1
    assert "safe output" in runner.stdin[0]
    turn = h.log.turn(output_turn)
    assert turn is not None
    assert turn.state == "done"
    assert h.log.pending_admin_followups() == []


PHOTO_KEY: Final = "expert:sketch:1"


def _file_item(
    update_id: int,
    file_id: str,
    name: str,
    *,
    caption: str | None = None,
    size: int | None = None,
) -> Inbound:
    return Inbound(
        update_id,
        InboundKind.FILE,
        OWNER,
        CHAT,
        str(update_id),
        text=caption,
        thread=THREAD,
        file=FileRef(id=file_id, name=name, size=size),
    )


def _conversation_directory(h: Harness, root: Path) -> Path:
    conversation = h.log.at_thread(THREAD)
    assert conversation is not None
    return root / "conversations" / str(conversation.id)


def _body(h: Harness, notice_key: str) -> str:
    row = h.database.connection.execute(
        "SELECT body FROM messages WHERE notice_key = ?", (notice_key,)
    ).fetchone()
    return row["body"]


def test_an_attached_file_is_saved_and_its_turn_names_its_absolute_path(
    build: Callable[..., Harness], tmp_path: Path
) -> None:
    runner = Runner()
    downloads = FakeDownloads({"file-1": b"attached"})
    h = build(ladder(runner), downloads=downloads)
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    item = _file_item(2, "file-1", "AGENTS.md", caption=MESSAGES[1])
    asyncio.run(h.turns.handle(item))

    saved = _conversation_directory(h, tmp_path) / "files" / "2-AGENTS.md"
    assert saved.read_bytes() == b"attached"
    assert downloads.fetched == ["file-1"]
    assert MESSAGES[1] in runner.stdin[1]
    assert str(saved) in runner.stdin[1]
    assert "expert:answer:2" in _answer_keys(h)


def test_a_repeated_file_update_makes_no_second_turn(build: Callable[..., Harness]) -> None:
    runner = Runner()
    h = build(ladder(runner), downloads=FakeDownloads({"file-1": b"once"}))
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    item = _file_item(2, "file-1", "a.txt")

    asyncio.run(h.turns.handle(item))
    asyncio.run(h.turns.handle(item))

    assert len(runner.stdin) == 2
    assert h.log.turn(2) is not None


@pytest.mark.parametrize(
    ("file_size", "refused"),
    [(DOWNLOAD_LIMIT_BYTES + 1, frozenset[str]()), (None, frozenset({"file-1"}))],
    ids=["over-the-limit", "refused-by-the-channel"],
)
def test_a_file_not_saved_gets_a_notice_and_no_turn(
    build: Callable[..., Harness],
    file_size: int | None,
    refused: frozenset[str],
) -> None:
    downloads = FakeDownloads({"file-1": b"unused"}, refused=refused)
    h = build(ladder(Runner()), downloads=downloads)
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    asyncio.run(h.turns.handle(_file_item(2, "file-1", "big.zip", size=file_size)))

    assert h.log.turn(2) is None
    assert "expert:file:2" in _answer_keys(h)
    assert downloads.fetched == ([] if file_size is not None else ["file-1"])


def test_a_file_that_fails_for_a_passing_reason_is_tried_again(
    build: Callable[..., Harness],
) -> None:
    downloads = FakeDownloads({"file-1": b"later"}, unavailable=frozenset({"file-1"}))
    h = build(ladder(Runner()), downloads=downloads)
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    item = _file_item(2, "file-1", "a.txt")

    with pytest.raises(ChannelUnavailableError):
        asyncio.run(h.turns.handle(item))
    assert h.log.turn(2) is None

    asyncio.run(h.turns.handle(item))

    assert h.log.turn(2) is not None
    assert downloads.fetched == ["file-1", "file-1"]


def test_a_file_outside_an_expert_topic_is_not_run_as_a_command() -> None:
    database = Database.open(":memory:")
    outbox = JournalOutbox(database, MessageStore(database, Reporter()))
    replies = Replies(
        cast("ReplyLog", None),
        outbox,
        Clock(),
        {},
        channel=CHANNEL,
        commands=cast("Commands", None),
        goal=cast("GoalCommand", None),
    )
    caption = Inbound(
        9, InboundKind.FILE, OWNER, OWNER, "9", text="/status", file=FileRef(id="f", name="a")
    )

    asyncio.run(replies.handle(caption))
    pending = outbox.pending()
    database.close()

    assert pending == []


def test_a_requested_sketch_is_kept_with_the_files_and_sent_as_a_photo(
    build: Callable[..., Harness], tmp_path: Path
) -> None:
    sketcher = FakeSketcher(tmp_path / "codex" / "pic.png")
    h = build(ladder(Runner(answer={**ANSWER, "image": "a box diagram"})), sketcher=sketcher)

    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    kept = _conversation_directory(h, tmp_path) / "files" / "sketch-1.png"
    assert sketcher.descriptions == ["a box diagram"]
    assert kept.read_bytes() == PNG
    assert not sketcher.made.exists()
    assert json.loads(_body(h, PHOTO_KEY))["photo"] == str(kept)


def test_a_sketch_is_drawn_once_however_often_its_turn_is_delivered(
    build: Callable[..., Harness], tmp_path: Path
) -> None:
    sketcher = FakeSketcher(tmp_path / "codex" / "pic.png")
    h = build(ladder(Runner(answer={**ANSWER, "image": "a graph"})), sketcher=sketcher)
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))
    conversation = h.log.at_thread(THREAD)
    assert conversation is not None

    asyncio.run(h.turns.run(conversation.id, 1))

    assert sketcher.descriptions == ["a graph"]


@pytest.mark.parametrize(
    ("description", "sketcher_error"),
    [
        (f"a diagram with {SECRETS['sk_key']}", None),
        ("a graph", "codex did not finish the run"),
    ],
    ids=["secret-in-description", "failed-run"],
)
def test_a_sketch_not_drawn_gets_a_notice_and_no_photo(
    build: Callable[..., Harness],
    tmp_path: Path,
    description: str,
    sketcher_error: str | None,
) -> None:
    sketcher = FakeSketcher(tmp_path / "pic.png", error=sketcher_error)
    h = build(ladder(Runner(answer={**ANSWER, "image": description})), sketcher=sketcher)

    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    assert PHOTO_KEY in _answer_keys(h)
    assert json.loads(_body(h, PHOTO_KEY))["photo"] is None
    assert not (_conversation_directory(h, tmp_path) / "files" / "sketch-1.png").exists()


def test_a_sketch_without_a_codex_rung_gets_a_notice(build: Callable[..., Harness]) -> None:
    h = build(ladder(Runner(answer={**ANSWER, "image": "a graph"})))

    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    assert PHOTO_KEY in _answer_keys(h)
    assert json.loads(_body(h, PHOTO_KEY))["photo"] is None


def test_a_sketch_never_overwrites_an_attachment_of_the_same_turn(
    build: Callable[..., Harness], tmp_path: Path
) -> None:
    downloads = FakeDownloads({"file-1": b"attached"})
    sketcher = FakeSketcher(tmp_path / "codex" / "pic.png")
    h = build(
        ladder(Runner(answer={**ANSWER, "image": "a graph"})),
        downloads=downloads,
        sketcher=sketcher,
    )
    asyncio.run(h.ask.handle(_item(1, f"/expert {MESSAGES[0]}"), None))

    asyncio.run(h.turns.handle(_file_item(2, "file-1", "sketch.png", caption=MESSAGES[1])))

    folder = _conversation_directory(h, tmp_path) / "files"
    assert (folder / "2-sketch.png").read_bytes() == b"attached"
    assert (folder / "sketch-2.png").read_bytes() == PNG
    assert json.loads(_body(h, "expert:sketch:2"))["photo"] == str(folder / "sketch-2.png")
