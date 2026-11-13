"""Submit all and Reject all on an Expert answer's task proposals, with fake Shikiru clients."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.expert_proposals import action_key, answer_key
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.callback import reject_all_data, submit_all_data
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertTask
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    Ack,
    AckStatus,
    ChannelRefusedError,
    Message,
)
from tests.support.expert_proposals import expert_proposals

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from shikiri_yaku.app.expert_proposals import ExpertProposals
    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import ShikiruClient

OWNER: Final = 1001
CHAT: Final = -100123
THREAD: Final = 22
CHANNEL: Final = "telegram"
NOW: Final = datetime(2026, 1, 1, tzinfo=UTC)
FIRST: Final = 1
FIRST_REF: Final = f"action-{FIRST}"
REFUSED: Final = "fake channel refusal"


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


class CrashError(RuntimeError):
    """The fake Shikiru or the journal dies at the point the test names."""


class FakeShikiru:
    """A fake Shikiru: it records each request, and crashes on the call numbered `crash_on`."""

    def __init__(self, *, crash_on: int | None = None) -> None:
        self.requests: list[str] = []
        self._crash_on = crash_on

    async def submit_task(self, request: str) -> Ack:
        self.requests.append(request)
        if len(self.requests) == self._crash_on:
            raise CrashError
        return Ack(
            id=len(self.requests),
            status=AckStatus.DONE,
            result={"task_id": f"T-{request}"},
            message=None,
        )


class FakeChannel:
    def __init__(self, *, delay_on: int | None = None, refuse_on: int | None = None) -> None:
        self.sent: list[tuple[Destination, Message]] = []
        self.attempts: list[tuple[Destination, Message]] = []
        self.delay_on = delay_on
        self.refuse_on = refuse_on
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, destination: Destination, message: Message) -> str:
        self.attempts.append((destination, message))
        attempt = len(self.attempts)
        if attempt == self.delay_on:
            self.started.set()
            await self.release.wait()
        if attempt == self.refuse_on or len(message.shown()) > MESSAGE_TEXT_LIMIT:
            raise ChannelRefusedError(REFUSED)
        self.sent.append((destination, message))
        return f"channel-{attempt}"

    async def edit(self, message_ref: str, message: Message) -> None:
        raise NotImplementedError

    async def delete(self, message_ref: str) -> None:
        raise NotImplementedError


class FlakyFinal(JournalFinalEdits):
    """Final edits that crash on the next `failures` settles, as a crash mid-recovery would."""

    def __init__(self, database: Database) -> None:
        replies = JournalReplies(
            database, SignalStore(database), OpStore(database), channel_name=CHANNEL
        )
        super().__init__(database, replies)
        self.failures = 0

    def settle_notice(
        self, notice_key: str, message: Message, *, conversation_id: int | None = None
    ) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise CrashError
        super().settle_notice(notice_key, message, conversation_id=conversation_id)


class Harness:
    def __init__(self, clients: Mapping[str, FakeShikiru]) -> None:
        self.database = Database.open(":memory:")
        _DATABASES.append(self.database)
        self.conversations = Conversations(self.database)
        self.messages = MessageStore(self.database, Reporter())
        self.outbox = JournalOutbox(self.database, self.messages)
        self.final = FlakyFinal(self.database)
        self.clients = clients
        self.conversation_id = 0
        self.proposals = self.restart()

    def restart(self) -> ExpertProposals:
        """Return the proposals over the same journal, as a process after a restart has them."""
        return expert_proposals(
            self.database,
            self.conversations,
            self.outbox,
            Clock(),
            cast("Mapping[str, ShikiruClient]", self.clients),
            channel=CHANNEL,
            group_id=CHAT,
            final=self.final,
        )

    def answer(
        self,
        update_id: int,
        tasks: list[tuple[str, str]],
        *,
        title: str = "",
        reply: str = "Plan",
        deliver: bool = True,
    ) -> None:
        """Deliver an answer with these (component, text) tasks, as an Expert turn does.

        Each task's title is its text, unless `title` is given. The answer's message is then sent,
        so that a tap from it counts.
        """
        if update_id == FIRST:
            self.conversations.create(
                update_id, chat=CHAT, thread=None, text="ask", scope=list(self.clients), now=NOW
            )
            self.conversations.opened(update_id, THREAD)
            conversation = self.conversations.get(update_id)
            assert conversation is not None
            self.conversation_id = conversation.id
        else:
            self.conversations.add_turn(self.conversation_id, update_id, "ask", NOW)
        answer = ExpertAnswer(
            reply=reply,
            title="Idea",
            tasks=[
                ExpertTask(component=name, title=title or text, text=text) for name, text in tasks
            ],
            findings="",
        )
        self.conversations.start(update_id)
        self.conversations.finish(update_id, answer.model_dump_json(), None, None, NOW)
        self._queue(update_id, deliver=deliver)

    def deliver_again(self, update_id: int) -> None:
        """Deliver a finished turn's answer a second time, as a start that re-delivers it does."""
        turn = self.conversations.turn(update_id)
        assert turn is not None
        assert turn.answer is not None
        self._queue(update_id)

    def _queue(self, update_id: int, *, deliver: bool = True) -> None:
        self.proposals.queue_answer(update_id)
        if not deliver:
            return
        rows = self.database.connection.execute(
            "SELECT id, notice_key, body FROM messages WHERE notice_key = ?"
            " OR notice_key LIKE ? ORDER BY id",
            (answer_key(update_id), f"{answer_key(update_id)}:%"),
        ).fetchall()
        for row in rows:
            if row["body"] is None:
                continue
            key = str(row["notice_key"])
            if key == action_key(update_id):
                ref = f"action-{update_id}"
            elif key == answer_key(update_id):
                ref = f"answer-{update_id}"
            else:
                ref = f"answer-{update_id}-{key.rsplit(':', 1)[-1]}"
            self.messages.confirm_delivery(int(row["id"]), ref)

    def _message_id(self, key: str) -> int:
        row = self.database.connection.execute(
            "SELECT id FROM messages WHERE notice_key = ?", (key,)
        ).fetchone()
        assert row is not None
        return int(row[0])

    def count(self, sql: str, *params: object) -> int:
        row = self.database.connection.execute(sql, params).fetchone()
        return int(row[0])

    def final_body(self, update_id: int) -> str:
        """Return the text the answer's message is settled to, which the PO sees after an edit."""
        row = self.database.connection.execute(
            "SELECT final_body FROM messages WHERE notice_key = ?", (action_key(update_id),)
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        return str(row[0])

    def body(self, update_id: int) -> str:
        """Return the text the answer's message was first sent with, buttons included."""
        row = self.database.connection.execute(
            "SELECT body FROM messages WHERE notice_key = ?", (action_key(update_id),)
        ).fetchone()
        assert row is not None
        return str(row[0])

    def texts(self, update_id: int) -> list[str]:
        """Return all answer and task-result texts in their current visible form."""
        rows = self.database.connection.execute(
            "SELECT COALESCE(final_body, body) AS body FROM messages"
            " WHERE notice_key = ? OR notice_key = ? OR notice_key LIKE ? OR notice_key LIKE ?"
            " ORDER BY id",
            (
                answer_key(update_id),
                action_key(update_id),
                f"{answer_key(update_id)}:part:%",
                f"ask:proposal:{update_id}:result:%",
            ),
        ).fetchall()
        return [json.loads(row["body"])["text"] for row in rows if row["body"]]

    def tap(self, update_id: int, data: str, *, ref: str = FIRST_REF) -> None:
        """Tap a button on the message `ref`; the answer of FIRST by default."""
        item = Inbound(
            update_id=update_id,
            kind=InboundKind.TAP,
            sender=OWNER,
            chat=CHAT,
            message_ref=ref,
            data=data,
            thread=THREAD,
        )
        asyncio.run(self.proposals.tap(item))

    def said(self) -> int:
        """Count the replies the PO was given in the topic about a proposal."""
        return self.count(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key LIKE 'expert:proposal:%'"
        )


def test_submit_all_sends_each_task_once_to_the_component_it_names() -> None:
    alpha, beta = FakeShikiru(), FakeShikiru()
    harness = Harness({"alpha": alpha, "beta": beta})
    harness.answer(FIRST, [("alpha", "first"), ("beta", "second")])
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.requests == ["first"]
    assert beta.requests == ["second"]
    final = harness.count(
        "SELECT COUNT(*) FROM messages WHERE notice_key = ? AND final_body LIKE '%T-first%'"
        " AND final_body LIKE '%T-second%'",
        action_key(FIRST),
    )
    assert final == 1


def test_a_second_tap_submits_nothing_and_says_so() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, submit_all_data(FIRST))
    harness.tap(3, submit_all_data(FIRST))
    harness.tap(4, reject_all_data(FIRST))
    assert alpha.requests == ["first"]
    assert harness.said() == 2


def test_a_tap_after_a_restart_submits_nothing() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.requests == ["first"]


def test_a_crash_leaves_the_rest_unsent_and_tells_the_po() -> None:
    alpha = FakeShikiru(crash_on=2)
    beta = FakeShikiru()
    harness = Harness({"alpha": alpha, "beta": beta})
    harness.answer(FIRST, [("alpha", "one"), ("alpha", "two"), ("beta", "three")])
    with pytest.raises(CrashError):
        harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.requests == ["one", "two"]
    assert beta.requests == []
    assert (
        harness.count(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ?",
            f"ask:proposal:{FIRST}:interrupted",
        )
        == 1
    )


def test_a_tap_on_a_voided_proposal_submits_nothing_and_says_so() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.answer(2, [("alpha", "second")])
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == []
    assert harness.said() == 1


def test_a_tap_on_a_rejected_proposal_submits_nothing_and_says_so() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, reject_all_data(FIRST))
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == []
    assert harness.said() == 1


def test_a_replayed_answer_does_not_void_a_newer_proposal() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.answer(2, [("alpha", "second")])
    harness.deliver_again(FIRST)
    harness.tap(3, submit_all_data(2), ref="action-2")
    harness.tap(4, submit_all_data(FIRST))
    assert alpha.requests == ["second"]
    assert harness.said() == 1


def test_a_tap_from_another_message_submits_nothing_and_leaves_the_proposal_open() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, submit_all_data(FIRST), ref="elsewhere")
    harness.tap(3, reject_all_data(FIRST), ref="elsewhere")
    assert alpha.requests == []
    assert harness.said() == 0
    harness.tap(4, submit_all_data(FIRST))
    assert alpha.requests == ["first"]


def test_a_long_proposal_is_split_across_messages_and_all_tasks_can_be_submitted() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    tasks = [f"task-{number}-{'x' * 300}" for number in range(30)]
    harness.answer(FIRST, [("alpha", task) for task in tasks])
    assert "Submit all" in harness.body(FIRST)
    before = "\n".join(harness.texts(FIRST))
    assert all(task in before for task in tasks)
    harness.tap(2, submit_all_data(FIRST), ref="answer-1-1")
    assert alpha.requests == []
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == tasks
    after = "\n".join(harness.texts(FIRST))
    assert all(f"submitted as task T-{task}" in after for task in tasks)


def test_action_waits_for_every_task_part_to_be_delivered() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    title = "x" * 2000
    tasks = [("alpha", f"task-{number}") for number in range(3)]
    harness.answer(FIRST, tasks, title=title, deliver=False)
    channel = FakeChannel(delay_on=2)
    sender = OutboxSender(harness.outbox, channel, Clock(), interval_s=1)

    async def exercise() -> None:
        sending = asyncio.create_task(sender.tick())
        await asyncio.wait_for(channel.started.wait(), timeout=1)
        assert len(channel.sent) == 1
        assert all(not message.buttons for _, message in channel.sent)
        await harness.proposals.tap(
            Inbound(
                update_id=2,
                kind=InboundKind.TAP,
                sender=OWNER,
                chat=CHAT,
                message_ref="channel-1",
                data=submit_all_data(FIRST),
                thread=THREAD,
            )
        )
        assert alpha.requests == []
        channel.release.set()
        await sending
        await sender.tick()
        action_index = next(
            index for index, (_, message) in enumerate(channel.sent) if message.buttons
        )
        task_parts = harness.count(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ? OR notice_key LIKE ?",
            answer_key(FIRST),
            f"{answer_key(FIRST)}:part:%",
        )
        assert action_index == task_parts
        assert all(not message.buttons for _, message in channel.sent[:action_index])
        action_ref = f"channel-{action_index + 1}"
        await harness.proposals.tap(
            Inbound(
                update_id=3,
                kind=InboundKind.TAP,
                sender=OWNER,
                chat=CHAT,
                message_ref=action_ref,
                data=submit_all_data(FIRST),
                thread=THREAD,
            )
        )

    asyncio.run(exercise())
    assert alpha.requests == [task for _, task in tasks]


def test_refused_task_part_never_offers_actions() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(
        FIRST,
        [("alpha", f"task-{number}") for number in range(3)],
        title="x" * 2000,
        deliver=False,
    )
    channel = FakeChannel(refuse_on=2)
    sender = OutboxSender(harness.outbox, channel, Clock(), interval_s=1)

    async def exercise() -> None:
        await sender.tick()
        assert all(not message.buttons for _, message in channel.sent)
        await sender.tick()

    asyncio.run(exercise())
    action = harness.database.connection.execute(
        "SELECT delivery FROM messages WHERE notice_key = ?", (action_key(FIRST),)
    ).fetchone()
    assert action is not None
    assert action[0] == "obsolete"
    assert all(not message.buttons for _, message in channel.sent)
    harness.tap(2, submit_all_data(FIRST), ref="channel-1")
    assert alpha.requests == []


def test_long_reply_and_task_parts_stay_within_channel_limit_when_delivered() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    task_title = "t" * 500
    harness.answer(
        FIRST,
        [("alpha", "work")],
        title=task_title,
        reply="r" * (MESSAGE_TEXT_LIMIT - 300),
        deliver=False,
    )
    channel = FakeChannel()
    sender = OutboxSender(harness.outbox, channel, Clock(), interval_s=1)

    async def exercise() -> None:
        await sender.tick()
        await sender.tick()

    asyncio.run(exercise())
    assert channel.sent
    assert all(len(message.shown()) <= MESSAGE_TEXT_LIMIT for _, message in channel.sent)
    assert task_title in "".join(message.text for _, message in channel.sent)
    assert channel.sent[-1][1].buttons


def test_full_reply_and_long_task_are_split_before_channel_delivery() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    task_title = "t" * 5000
    harness.answer(
        FIRST,
        [("alpha", "work")],
        title=task_title,
        reply="r" * 10_000,
        deliver=False,
    )
    channel = FakeChannel()
    sender = OutboxSender(harness.outbox, channel, Clock(), interval_s=1)

    async def exercise() -> None:
        await sender.tick()
        await sender.tick()

    asyncio.run(exercise())
    assert all(len(message.shown()) <= MESSAGE_TEXT_LIMIT for _, message in channel.sent)
    assert task_title in "".join(message.text for _, message in channel.sent)
    assert channel.sent[-1][1].buttons
    assert (
        harness.count(
            "SELECT COUNT(*) FROM messages WHERE (notice_key = ? OR notice_key LIKE ?)"
            " AND delivery = 'sent'",
            answer_key(FIRST),
            f"{answer_key(FIRST)}:part:%",
        )
        > 1
    )


def test_a_long_proposal_that_fits_shows_every_task_before_submission() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    tasks = [("alpha", f"task-{number}") for number in range(1, 11)]
    harness.answer(FIRST, tasks, title="x" * 240)
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.requests == [f"task-{number}" for number in range(1, 11)]
    final = harness.final_body(FIRST)
    assert final.count("submitted as task") == 10
    assert "T-task-10" in final


def test_a_long_task_id_is_shown_without_truncation() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    task = "x" * 100
    harness.answer(FIRST, [("alpha", task)])
    harness.tap(2, submit_all_data(FIRST))
    assert f"submitted as task T-{task}" in harness.final_body(FIRST)


def test_a_task_id_longer_than_a_message_is_shown_in_full_across_result_messages() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    task = "x" * 5000
    harness.answer(FIRST, [("alpha", task)])
    harness.tap(2, submit_all_data(FIRST))
    rows = harness.database.connection.execute(
        "SELECT json_extract(body, '$.text') FROM messages WHERE notice_key LIKE ? ORDER BY id",
        (f"ask:proposal:{FIRST}:result:%",),
    ).fetchall()
    result = "".join(str(row[0]) for row in rows).removeprefix("Task results:\n")
    assert f"submitted as task T-{task}" in result


def test_a_secret_in_a_long_task_is_refused_before_the_task_is_split() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    prefix = "1. alpha · "
    task = "x" * (MESSAGE_TEXT_LIMIT - len(prefix) - 5) + " sk-" + "a" * 20
    harness.answer(FIRST, [("alpha", task)])
    row = harness.database.connection.execute(
        "SELECT delivery, body FROM messages WHERE notice_key = ?", (answer_key(FIRST),)
    ).fetchone()
    assert row is not None
    assert row["delivery"] == "refused"
    assert row["body"] is None


def test_a_crash_after_the_submit_before_the_edit_is_made_up_on_restart() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.final.failures = 1
    with pytest.raises(CrashError):
        harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    assert "T-first" in harness.final_body(FIRST)
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == ["first"]


def test_a_crash_after_the_reject_before_the_edit_is_made_up_on_restart() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.final.failures = 1
    with pytest.raises(CrashError):
        harness.tap(2, reject_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    assert "Submit all" not in harness.final_body(FIRST)
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == []


def test_a_crash_after_the_void_before_the_edit_is_made_up_on_restart() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.final.failures = 1
    with pytest.raises(CrashError):
        harness.answer(2, [("alpha", "second")])
    harness.proposals = harness.restart()
    harness.proposals.recover()
    assert "Submit all" not in harness.final_body(FIRST)
    harness.tap(3, submit_all_data(FIRST))
    assert alpha.requests == []


def test_a_crash_while_recovering_is_made_up_on_the_next_restart() -> None:
    alpha = FakeShikiru(crash_on=2)
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "one"), ("alpha", "two")])
    with pytest.raises(CrashError):
        harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.final.failures = 1
    with pytest.raises(CrashError):
        harness.proposals.recover()
    harness.proposals = harness.restart()
    harness.proposals.recover()
    harness.proposals.recover()
    assert "Submit all" not in harness.final_body(FIRST)
    assert (
        harness.count(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ?",
            f"ask:proposal:{FIRST}:interrupted",
        )
        == 1
    )
    assert alpha.requests == ["one", "two"]
