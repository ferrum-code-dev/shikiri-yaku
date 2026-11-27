"""Submit all and Reject all on an Expert answer's task proposals, with fake Shikiru clients."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT, telegram_place
from shikiri_yaku.app.expert_admin import AdminAccess, AdminExecutor, recover_admin
from shikiri_yaku.app.expert_files import TaskFiles
from shikiri_yaku.app.expert_proposals import action_key, answer_key
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.callback import reject_all_data, submit_all_data, view_all_data
from shikiri_yaku.domain.expert import (
    MODE_ADMIN,
    MODE_EXPERT,
    AdminAnswer,
    ExpertAnswer,
    ExpertTask,
)
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.journal.proposals import JournalProposals
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    AdminCommandResult,
    ChannelError,
    ChannelRefusedError,
    Message,
    ShikiruLinkDownError,
    ShikiruRejectedError,
)
from tests.support.event_numbers import number_next_event
from tests.support.expert_proposals import expert_proposals
from tests.support.rooms import FakeRooms

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.app.expert_proposals import ExpertProposals
    from shikiri_yaku.domain.outbound import Purpose
    from shikiri_yaku.domain.place import Place
    from shikiri_yaku.ports import ShikiruClient, ShikiruError

OWNER: Final = 1001
ADMIN: Final = 1002
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
    """A fake Shikiru: it records each request, crashes on the call numbered `crash_on`, and
    raises `error` on the call numbered `error_on`."""

    def __init__(
        self,
        *,
        crash_on: int | None = None,
        error_on: int | None = None,
        error: type[ShikiruError] = ShikiruRejectedError,
    ) -> None:
        self.requests: list[str] = []
        self.modes: list[str | None] = []
        self._crash_on = crash_on
        self._error_on = error_on
        self._error = error

    async def submit_task(self, request: str, mode: str | None = None) -> Ack:
        self.requests.append(request)
        self.modes.append(mode)
        if len(self.requests) == self._crash_on:
            raise CrashError
        if len(self.requests) == self._error_on:
            raise self._error
        return Ack(
            id=len(self.requests),
            status=AckStatus.DONE,
            result={"task_id": f"T-{request}"},
            message=None,
        )


class FakeChannel:
    def __init__(self, *, delay_on: int | None = None, refuse_on: int | None = None) -> None:
        self.sent: list[tuple[Place, Message]] = []
        self.attempts: list[tuple[Place, Message]] = []
        self.delay_on = delay_on
        self.refuse_on = refuse_on
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, place: Place, message: Message) -> str:
        self.attempts.append((place, message))
        attempt = len(self.attempts)
        if attempt == self.delay_on:
            self.started.set()
            await self.release.wait()
        if attempt == self.refuse_on or len(message.shown()) > MESSAGE_TEXT_LIMIT:
            raise ChannelRefusedError(REFUSED)
        self.sent.append((place, message))
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
        super().__init__(database, replies, max_text=MESSAGE_TEXT_LIMIT)
        self.failures = 0

    def settle_notice(
        self, notice_key: str, message: Message, *, conversation_id: int | None = None
    ) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise CrashError
        super().settle_notice(notice_key, message, conversation_id=conversation_id)


class Harness:
    def __init__(
        self,
        clients: Mapping[str, FakeShikiru],
        *,
        mode: str = MODE_EXPERT,
        admins: frozenset[str] = frozenset(),
        files: TaskFiles | None = None,
    ) -> None:
        self.mode = mode
        self.admins = admins
        self.files = files
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
            access=AdminAccess(self.conversations, Clock(), admins=self.admins),
            files=self.files,
        )

    def answer(
        self,
        update_id: int,
        tasks: Sequence[tuple[str, str] | tuple[str, str, bool] | tuple[str, str, bool, list[str]]],
        *,
        title: str = "",
        reply: str = "Plan",
        deliver: bool = True,
    ) -> None:
        """Deliver an answer with these (component, text) tasks, as an Expert turn does.

        A task may add a third element for its planning marker and a fourth for its file names.
        Each task's title is its text, unless `title` is given. The answer's message is then sent,
        so that a tap from it counts.
        """
        if update_id == FIRST:
            self.conversations.create(
                update_id,
                origin=telegram_place(CHAT),
                text="ask",
                scope=list(self.clients),
                now=NOW,
                mode=self.mode,
            )
            self.conversations.opened(update_id, str(THREAD))
            conversation = self.conversations.get(update_id)
            assert conversation is not None
            self.conversation_id = conversation.id
        else:
            self.conversations.add_turn(self.conversation_id, update_id, "ask", NOW)
        answer = ExpertAnswer(
            reply=reply,
            title="Idea",
            tasks=[
                ExpertTask(
                    component=item[0],
                    title=title or item[1],
                    text=item[1],
                    planning=item[2] if len(item) > 2 else False,
                    files=item[3] if len(item) > 3 else [],
                )
                for item in tasks
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
                f"expert:proposal:{update_id}:result:%",
            ),
        ).fetchall()
        return [json.loads(row["body"])["text"] for row in rows if row["body"]]

    def tap(self, update_id: int, data: str, *, ref: str = FIRST_REF, sender: int = OWNER) -> None:
        """Tap a button on the message `ref`; the answer of FIRST by default."""
        item = Inbound(
            id=update_id,
            key=str(update_id),
            kind=InboundKind.TAP,
            sender=str(sender),
            place=telegram_place(CHAT, THREAD),
            message_ref=ref,
            data=data,
        )
        asyncio.run(self.proposals.tap(item))

    def views(self, tap_update_id: int) -> list[str]:
        """Return the texts that the View all tap with this update id sent, in order."""
        rows = self.database.connection.execute(
            "SELECT body FROM messages WHERE notice_key LIKE ? ORDER BY id",
            (f"expert:view:{tap_update_id}:%",),
        ).fetchall()
        return [json.loads(row["body"])["text"] for row in rows]

    def said(self) -> int:
        """Count the replies the PO was given in the topic about a proposal."""
        return self.count(
            "SELECT COUNT(*) FROM command_replies WHERE reply_key LIKE 'expert:proposal:%'"
        )


class _Uncopyable(TaskFiles):
    def copy_for_proposal(
        self, conversation_id: int, proposal_id: int, names: Iterable[str]
    ) -> dict[str, str]:
        del conversation_id, proposal_id, names
        raise OSError


def test_submit_all_names_each_task_only_its_own_copied_files(tmp_path: Path) -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha}, files=TaskFiles(tmp_path))
    harness.answer(FIRST, [("alpha", "first", False, ["1-photo.jpg"]), ("alpha", "second", False)])
    folder = tmp_path / "conversations" / str(harness.conversation_id) / "files"
    folder.mkdir(parents=True)
    (folder / "1-photo.jpg").write_bytes(b"photo")
    (folder / "2-notes.txt").write_bytes(b"notes")
    copy = tmp_path / "task_files" / str(FIRST) / "1-photo.jpg"

    harness.tap(2, submit_all_data(FIRST))

    assert copy.read_bytes() == b"photo"
    assert not (tmp_path / "task_files" / str(FIRST) / "2-notes.txt").exists()
    first, second = alpha.requests
    assert first.startswith("first")
    assert str(copy) in first
    assert second == "second"


def test_the_task_list_names_each_tasks_files(tmp_path: Path) -> None:
    harness = Harness({"alpha": FakeShikiru()}, files=TaskFiles(tmp_path))
    harness.answer(FIRST, [("alpha", "first", False, ["1-photo.jpg", "sketch-1.png"])])

    assert any("files: 1-photo.jpg, sketch-1.png" in text for text in harness.texts(FIRST))


def test_a_missing_named_file_submits_nothing_and_tells_the_po(tmp_path: Path) -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha}, files=TaskFiles(tmp_path))
    harness.answer(FIRST, [("alpha", "first", False, ["ghost.txt"])])

    harness.tap(2, submit_all_data(FIRST))

    assert alpha.requests == []
    assert (
        harness.count("SELECT COUNT(*) FROM command_replies WHERE reply_key = ?", "expert:files:2")
        == 1
    )


def test_a_failed_file_copy_submits_nothing_and_tells_the_po(tmp_path: Path) -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha}, files=_Uncopyable(tmp_path))
    harness.answer(FIRST, [("alpha", "first", False, ["1-photo.jpg"])])

    harness.tap(2, submit_all_data(FIRST))

    assert alpha.requests == []
    assert (
        harness.count("SELECT COUNT(*) FROM command_replies WHERE reply_key = ?", "expert:files:2")
        == 1
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


@pytest.mark.parametrize("settle", [submit_all_data(FIRST), reject_all_data(FIRST)])
def test_settling_edits_the_action_message_and_never_the_answer(settle: str) -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, settle)
    final_answer = harness.count(
        "SELECT COUNT(*) FROM messages WHERE notice_key = ? AND final_body IS NOT NULL",
        answer_key(FIRST),
    )
    final_action = harness.count(
        "SELECT COUNT(*) FROM messages WHERE notice_key = ? AND final_body IS NOT NULL",
        action_key(FIRST),
    )
    assert final_answer == 0
    assert final_action == 1


def test_only_a_listed_admin_may_settle_proposals_of_an_admin_conversation() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, submit_all_data(FIRST))
    harness.tap(3, reject_all_data(FIRST))
    assert alpha.requests == []
    assert harness.said() == 0
    harness.admins = frozenset()
    harness.proposals = harness.restart()
    harness.tap(4, submit_all_data(FIRST), sender=ADMIN)
    assert alpha.requests == []
    harness.admins = frozenset({str(ADMIN)})
    harness.proposals = harness.restart()
    harness.tap(5, submit_all_data(FIRST), sender=ADMIN)
    assert alpha.requests == ["first"]


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
            f"expert:proposal:{FIRST}:interrupted",
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
                id=2,
                key=str(2),
                kind=InboundKind.TAP,
                sender=str(OWNER),
                place=telegram_place(CHAT, THREAD),
                message_ref="channel-1",
                data=submit_all_data(FIRST),
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
                id=3,
                key=str(3),
                kind=InboundKind.TAP,
                sender=str(OWNER),
                place=telegram_place(CHAT, THREAD),
                message_ref=action_ref,
                data=submit_all_data(FIRST),
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


def submitted_count(text: str) -> tuple[int, int]:
    """Return the confirmed and total task counts that the submitted line shows."""
    match = re.search(r"(\d+) of (\d+) tasks", text)
    assert match is not None
    return int(match[1]), int(match[2])


def test_the_submitted_count_is_every_task_when_all_are_confirmed() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first"), ("alpha", "second")])
    harness.tap(2, submit_all_data(FIRST))
    assert submitted_count(harness.final_body(FIRST)) == (2, 2)


@pytest.mark.parametrize("error", [ShikiruRejectedError, ShikiruLinkDownError])
def test_the_submitted_count_leaves_out_a_task_that_failed_or_is_unknown(
    error: type[ShikiruError],
) -> None:
    harness = Harness({"alpha": FakeShikiru(error_on=2, error=error)})
    harness.answer(FIRST, [("alpha", "first"), ("alpha", "second")])
    harness.tap(2, submit_all_data(FIRST))
    assert submitted_count(harness.final_body(FIRST)) == (1, 2)


def test_a_submitted_proposal_replaces_the_action_message_without_the_reply() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first")], reply="Plan detail")
    assert "Plan detail" in "\n".join(harness.texts(FIRST))
    harness.tap(2, submit_all_data(FIRST))
    final = harness.final_body(FIRST)
    assert "1. alpha · first: submitted as task T-first" in final
    assert "Submitted: 1 of 1 tasks." in final
    assert "Plan detail" not in final
    assert "\n".join(harness.texts(FIRST)).count("Plan detail") == 1


def test_an_interrupted_proposal_replaces_the_action_message_without_the_reply() -> None:
    alpha = FakeShikiru(crash_on=2)
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "one"), ("alpha", "two")], reply="Plan detail")
    with pytest.raises(CrashError):
        harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    final = harness.final_body(FIRST)
    assert "1. alpha · one: submitted as task T-one" in final
    assert (
        "2. alpha · two: unknown, it may have reached Shikiru; not sent again, please check"
        in final
    )
    assert "Interrupted by a restart" in final
    assert "Plan detail" not in final
    assert "\n".join(harness.texts(FIRST)).count("Plan detail") == 1


def test_a_too_long_submitted_result_anchors_without_the_reply() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "x" * 5000)], reply="Plan detail")
    harness.tap(2, submit_all_data(FIRST))
    final = harness.final_body(FIRST)
    assert "Submitted: 1 of 1 tasks; results follow." in final
    assert "Plan detail" not in final


def test_a_rejected_proposal_replaces_the_action_message_without_the_reply() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first")], reply="Plan detail")
    harness.tap(2, reject_all_data(FIRST))
    final = harness.final_body(FIRST)
    assert json.loads(final)["text"] == "Rejected. Nothing submitted."
    assert "Plan detail" not in final


def test_a_task_id_longer_than_a_message_is_shown_in_full_across_result_messages() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    task = "x" * 5000
    harness.answer(FIRST, [("alpha", task)])
    harness.tap(2, submit_all_data(FIRST))
    rows = harness.database.connection.execute(
        "SELECT json_extract(body, '$.text') FROM messages WHERE notice_key LIKE ? ORDER BY id",
        (f"expert:proposal:{FIRST}:result:%",),
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


def test_the_action_message_offers_view_all_and_no_longer_points_above() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first")])
    body = harness.body(FIRST)
    assert "View all" in body
    assert "above" not in body


def test_view_all_sends_each_component_together_with_the_list_numbers() -> None:
    harness = Harness({"alpha": FakeShikiru(), "beta": FakeShikiru()})
    harness.answer(
        FIRST, [("alpha", "one", False, ["a.py"]), ("beta", "two"), ("alpha", "three", True)]
    )
    harness.tap(2, view_all_data(FIRST))
    first, second, third = harness.views(2)
    assert "Project alpha tasks:" in first
    assert "1. one" in first
    assert "files: a.py" in first
    assert "3. three (planning)" in second
    assert "Project" not in second
    assert "Project beta tasks:" in third
    assert "2. two" in third


def test_a_long_task_continues_in_further_messages_with_no_word_lost() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    words = [f"w{number}" for number in range(1000)]
    harness.answer(FIRST, [("alpha", " ".join(words), False)], title="short")
    harness.tap(2, view_all_data(FIRST))
    sent = harness.views(2)
    assert len(sent) > 1
    assert all(len(text) <= MESSAGE_TEXT_LIMIT for text in sent)
    assert [word for text in sent for word in text.split() if word.startswith("w")] == words


def test_a_secret_in_any_task_sends_no_task_and_tells_the_po() -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(
        FIRST, [("alpha", "harmless", False), ("alpha", "key sk-" + "a" * 20, False)], title="short"
    )
    harness.tap(2, view_all_data(FIRST))
    sent = harness.views(2)
    assert len(sent) == 1
    assert "harmless" not in sent[0]


def test_view_all_submits_nothing_and_each_tap_sends_the_tasks_once() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, view_all_data(FIRST))
    harness.tap(2, view_all_data(FIRST))
    assert len(harness.views(2)) == 1
    assert alpha.requests == []
    harness.tap(3, view_all_data(FIRST))
    assert len(harness.views(3)) == 1
    harness.tap(4, submit_all_data(FIRST))
    assert alpha.requests == ["first"]


@pytest.mark.parametrize("settle", [submit_all_data(FIRST), reject_all_data(FIRST)])
def test_view_all_after_a_decision_or_from_another_message_sends_nothing(settle: str) -> None:
    harness = Harness({"alpha": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, view_all_data(FIRST), ref="elsewhere")
    assert harness.views(2) == []
    assert harness.said() == 0
    harness.tap(3, settle)
    harness.tap(4, view_all_data(FIRST))
    assert harness.views(4) == []
    assert harness.said() == 1
    assert "View all" not in harness.final_body(FIRST)


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
            f"expert:proposal:{FIRST}:interrupted",
        )
        == 1
    )
    assert alpha.requests == ["one", "two"]


def test_admin_op_decisions_are_conditional_and_duplicate_safe() -> None:
    database = Database.open(":memory:")
    _DATABASES.append(database)
    conversations = Conversations(database)
    conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    conversations.opened(1, str(THREAD))
    conversation = conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(database, OpStore(database), channel_name=CHANNEL)
    op, voided = journal.intend_admin(conversation.id, 1, "echo test", "reason", NOW)
    assert not voided
    cutoff = NOW - timedelta(minutes=10)
    assert not journal.decide_admin(
        op, 2, str(OWNER), admin=False, approve=True, now=NOW, cutoff=cutoff
    )
    assert journal.decide_admin(op, 3, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=cutoff)
    assert not journal.decide_admin(
        op, 3, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=cutoff
    )
    row = database.connection.execute(
        "SELECT state, payload FROM ops WHERE id = ?", (op,)
    ).fetchone()
    assert row["state"] == "approved"
    assert json.loads(row["payload"])["approver"] == str(ADMIN)
    assert json.loads(row["payload"])["update_id"] == 3
    expired, _ = journal.intend_admin(conversation.id, 4, "echo old", "reason", NOW)
    assert not journal.decide_admin(
        expired, 5, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW
    )
    new, replaced = journal.intend_admin(conversation.id, 6, "echo new", "reason", NOW)
    assert replaced == [expired]
    assert journal.decide_admin(
        new, 7, str(ADMIN), admin=True, approve=False, now=NOW, cutoff=cutoff
    )
    assert not journal.decide_admin(
        new, 8, str(ADMIN), admin=True, approve=False, now=NOW, cutoff=cutoff
    )


def test_admin_expiry_only_changes_pending_operations() -> None:
    database = Database.open(":memory:")
    _DATABASES.append(database)
    conversations = Conversations(database)
    conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    conversations.opened(1, str(THREAD))
    conversation = conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(database, OpStore(database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(conversation.id, 1, "echo test", "reason", NOW)
    assert journal.expire_admin(NOW - timedelta(seconds=1), NOW) == []
    assert journal.expire_admin(NOW, NOW) == [op]
    assert journal.expire_admin(NOW, NOW) == []
    assert not journal.decide_admin(
        op, 2, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW - timedelta(minutes=1)
    )


@pytest.mark.parametrize("verb", ["u", "v"])
def test_admin_proposal_delivery_and_decision(verb: str) -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.answer(FIRST, [])
    answer = AdminAnswer(
        reply="Ready", title="", tasks=[], findings="", command="echo hi", reason="test"
    )
    harness.database.connection.execute(
        "UPDATE turns SET answer = ? WHERE update_id = ?", (answer.model_dump_json(), FIRST)
    )
    harness.proposals.queue_admin(FIRST)
    op = harness.database.connection.execute(
        "SELECT id, state FROM ops WHERE kind = 'admin'"
    ).fetchone()
    assert op["state"] == "intended"
    key = f"admin:proposal:{op['id']}"
    message = harness.database.connection.execute(
        "SELECT id, body FROM messages WHERE notice_key = ?", (key,)
    ).fetchone()
    body = json.loads(message["body"])
    assert body["text"].endswith("echo hi")
    assert [button["data"] for button in body["buttons"]] == [f"u:{op['id']}", f"v:{op['id']}"]
    harness.messages.confirm_delivery(message["id"], "admin-ref")
    called: list[tuple[int, int]] = []

    async def run(conversation_id: int, update_id: int) -> None:
        called.append((conversation_id, update_id))
        harness.database.connection.execute(
            "UPDATE turns SET state = 'done' WHERE update_id = ?", (update_id,)
        )

    async def tap(update_id: int, sender: int) -> None:
        item = Inbound(
            id=update_id,
            key=str(update_id),
            kind=InboundKind.TAP,
            sender=str(sender),
            place=telegram_place(CHAT, THREAD),
            message_ref="admin-ref",
            data=f"{verb}:{op['id']}",
        )
        assert await harness.proposals.admin_tap(item, run)

    asyncio.run(tap(2, OWNER))
    assert (
        harness.database.connection.execute(
            "SELECT state FROM ops WHERE id = ?", (op["id"],)
        ).fetchone()[0]
        == "intended"
    )
    asyncio.run(tap(3, ADMIN))
    asyncio.run(tap(3, ADMIN))
    state = harness.database.connection.execute(
        "SELECT state FROM ops WHERE id = ?", (op["id"],)
    ).fetchone()[0]
    assert state == ("approved" if verb == "u" else "declined")
    # keep: the replay of a decline tap resumes its turn, which may be done and still undelivered
    cid = harness.conversation_id
    assert called == ([] if verb == "u" else [(cid, 3), (cid, 3)])
    final = harness.database.connection.execute(
        "SELECT final_body FROM messages WHERE notice_key = ?", (key,)
    ).fetchone()[0]
    assert json.loads(final)["buttons"] == []


def test_new_admin_proposal_voids_previous_message_and_expiry_removes_buttons() -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.answer(FIRST, [])
    answer = AdminAnswer(
        reply="Ready", title="", tasks=[], findings="", command="echo first", reason="test"
    )
    harness.database.connection.execute(
        "UPDATE turns SET answer = ? WHERE update_id = ?", (answer.model_dump_json(), FIRST)
    )
    harness.proposals.queue_admin(FIRST)
    first = harness.database.connection.execute(
        "SELECT id FROM ops WHERE kind = 'admin'"
    ).fetchone()[0]
    harness.conversations.add_turn(harness.conversation_id, 2, "again", NOW)
    harness.conversations.start(2)
    answer.command = "echo second"
    harness.conversations.finish(2, answer.model_dump_json(), None, None, NOW)
    harness.proposals.queue_admin(2)
    rows = harness.database.connection.execute(
        "SELECT id, state FROM ops WHERE kind = 'admin' ORDER BY id"
    ).fetchall()
    assert [row["state"] for row in rows] == ["void", "intended"]
    message = harness.database.connection.execute(
        "SELECT final_body FROM messages WHERE notice_key = ?", (f"admin:proposal:{first}",)
    ).fetchone()
    assert json.loads(message[0])["buttons"] == []
    old = (NOW - timedelta(minutes=10)).isoformat()
    harness.database.connection.execute(
        "UPDATE ops SET created_at = ? WHERE id = ?", (old, rows[1]["id"])
    )
    harness.proposals.expire_admin()
    row = harness.database.connection.execute(
        "SELECT state FROM ops WHERE id = ?", (rows[1]["id"],)
    ).fetchone()
    assert row[0] == "expired"
    message = harness.database.connection.execute(
        "SELECT final_body FROM messages WHERE notice_key = ?", (f"admin:proposal:{rows[1]['id']}",)
    ).fetchone()
    assert json.loads(message[0])["buttons"] == []


@pytest.mark.parametrize("action", ["void", "expire", "approve", "decline", "close", "idle"])
def test_admin_state_and_final_edit_commit_together(action: str) -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.answer(FIRST, [])
    answer = AdminAnswer(
        reply="Ready", title="", tasks=[], findings="", command="echo first", reason="test"
    )
    harness.database.connection.execute(
        "UPDATE turns SET answer = ? WHERE update_id = ?", (answer.model_dump_json(), FIRST)
    )
    harness.proposals.queue_admin(FIRST)
    row = harness.database.connection.execute("SELECT id FROM ops WHERE kind = 'admin'").fetchone()
    op_id = row["id"]
    key = f"admin:proposal:{op_id}"
    message_id = harness.database.connection.execute(
        "SELECT id FROM messages WHERE notice_key = ?", (key,)
    ).fetchone()[0]
    harness.messages.confirm_delivery(message_id, "admin-ref")
    item = Inbound(
        id=2,
        key=str(2),
        kind=InboundKind.TEXT,
        sender=str(ADMIN),
        place=telegram_place(CHAT, THREAD),
        message_ref="2",
        text="new",
    )
    number_next_event(harness.database, CHANNEL, item.id)
    JournalInbound(harness.database).record_batch(CHANNEL, [item], "3", NOW)
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)

    def change() -> None:
        if action == "void":
            journal.intend_admin(harness.conversation_id, 2, "echo next", "test", NOW)
        elif action == "expire":
            journal.expire_admin(NOW, NOW)
        elif action == "close":
            harness.conversations.begin_ending(
                harness.conversation_id, NOW, sender=str(ADMIN), update_id=2
            )
        elif action == "idle":
            harness.conversations.begin_ending(harness.conversation_id, NOW)
        else:
            journal.decide_admin(
                op_id,
                2,
                str(ADMIN),
                admin=True,
                approve=action == "approve",
                now=NOW,
                cutoff=NOW - timedelta(minutes=1),
            )

    harness.database.connection.execute(
        "CREATE TRIGGER crash_edit BEFORE UPDATE OF final_body ON messages "
        "BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
    )
    with pytest.raises(Exception, match="interrupted"):
        change()
    row = harness.database.connection.execute(
        "SELECT state FROM ops WHERE id = ?", (op_id,)
    ).fetchone()
    assert row["state"] == "intended"
    assert (
        harness.database.connection.execute(
            "SELECT final_body FROM messages WHERE id = ?", (message_id,)
        ).fetchone()[0]
        is None
    )
    harness.database.connection.execute("DROP TRIGGER crash_edit")
    change()
    row = harness.database.connection.execute(
        "SELECT state, payload FROM ops WHERE id = ?", (op_id,)
    ).fetchone()
    assert (
        row["state"]
        == {
            "void": "void",
            "expire": "expired",
            "approve": "approved",
            "decline": "declined",
            "close": "void",
            "idle": "void",
        }[action]
    )
    audit = json.loads(row["payload"])
    assert audit["update_id"] == (None if action in ("expire", "idle") else 2)
    assert audit["actor"] == (None if action in ("expire", "idle") else str(ADMIN))
    assert audit["approver"] == (str(ADMIN) if action in ("approve", "decline") else None)
    final = harness.database.connection.execute(
        "SELECT final_body FROM messages WHERE id = ?", (message_id,)
    ).fetchone()[0]
    assert json.loads(final)["buttons"] == []


def test_admin_issue_and_recovery_never_repeat() -> None:
    database = Database.open(":memory:")
    _DATABASES.append(database)
    conversations = Conversations(database)
    conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    conversations.opened(1, str(THREAD))
    conversation = conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(database, OpStore(database), channel_name=CHANNEL)
    ops: list[int] = []
    for update in (10, 20, 30, 40):
        op, _ = journal.intend_admin(conversation.id, update, "echo test", "reason", NOW)
        assert journal.decide_admin(
            op,
            update + 1,
            str(ADMIN),
            admin=True,
            approve=True,
            now=NOW,
            cutoff=NOW - timedelta(minutes=1),
        )
        ops.append(op)
    assert journal.issue_admin(ops[0], NOW) == (conversation.id, "echo test")
    assert journal.issue_admin(ops[0], NOW) is None
    turn = journal.finish_admin(ops[0], NOW, "safe output", 4, 0.1, cut=True)
    assert turn is not None
    recorded = conversations.turn(turn)
    assert recorded is not None
    assert recorded.text == "safe output"
    assert journal.issue_admin(ops[3], NOW) is not None
    assert journal.finish_admin(ops[3], NOW, "timed out", None, 1.0, cut=False) is not None
    failed = journal.admin_op(ops[3])
    assert failed is not None
    assert failed[1] == "failed"
    assert journal.issue_admin(ops[1], NOW) is not None
    assert journal.recover_admin(NOW) == [
        (ops[1], conversation.id, "unknown"),
        (ops[2], conversation.id, "void"),
    ]
    assert journal.recover_admin(NOW) == []
    assert journal.issue_admin(ops[1], NOW) is None
    assert journal.issue_admin(ops[2], NOW) is None
    row = database.connection.execute(
        "SELECT state, payload FROM ops WHERE id = ?", (ops[0],)
    ).fetchone()
    assert row["state"] == "confirmed"
    assert json.loads(row["payload"])["exit_code"] == 4
    assert json.loads(row["payload"])["cut"] == 1


def test_admin_executor_serializes_claims_and_delivers_one_turn_per_approval(
    tmp_path: Path,
) -> None:

    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    ops: list[int] = []
    for update in (10, 20):
        op, _ = journal.intend_admin(conversation.id, update, f"echo {update}", "reason", NOW)
        assert journal.decide_admin(
            op,
            update + 1,
            str(ADMIN),
            admin=True,
            approve=True,
            now=NOW,
            cutoff=NOW - timedelta(minutes=1),
        )
        ops.append(op)

    async def scenario() -> None:
        class Lock:
            def __init__(self) -> None:
                self.lock = asyncio.Lock()

            def hold(self) -> asyncio.Lock:
                return self.lock

        class Runner:
            def __init__(self) -> None:
                self.calls: list[tuple[list[str], Path]] = []
                self.active = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def run_admin(
                self,
                argv: Sequence[str],
                *,
                timeout_s: float,
                cwd: Path,
                max_bytes: int,
                secrets: Sequence[str],
            ) -> AdminCommandResult:
                assert timeout_s == 1
                assert max_bytes == 100
                assert not secrets
                self.active += 1
                assert self.active == 1
                self.calls.append((list(argv), cwd))
                self.started.set()
                await self.release.wait()
                self.active -= 1
                return AdminCommandResult(returncode=7, output="safe", cut=False)

        runner = Runner()
        executor = AdminExecutor(  # noqa: S604 -- fake runner
            journal,
            harness.conversations,
            runner,
            Lock(),
            harness.outbox,
            Clock(),
            directory=tmp_path,
            shell="/bin/sh",
            timeout_s=1,
            max_bytes=100,
            secrets=(),
            rooms=FakeRooms(CHAT),
            max_text=MESSAGE_TEXT_LIMIT,
        )
        turns: list[tuple[int, int]] = []

        async def run(conversation_id: int, turn: int) -> None:
            turns.append((conversation_id, turn))

        tasks = [asyncio.create_task(executor.execute(op, run)) for op in (ops[0], ops[0], ops[1])]
        await runner.started.wait()
        assert len(runner.calls) == 1
        runner.release.set()
        await asyncio.gather(*tasks)
        assert runner.calls == [
            (["/bin/sh", "-l", "-c", "echo 10"], tmp_path),
            (["/bin/sh", "-l", "-c", "echo 20"], tmp_path),
        ]
        assert len(turns) == 2
        for _, turn in turns:
            recorded = harness.conversations.turn(turn)
            assert recorded is not None
            assert recorded.text.endswith("\nsafe")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("returncode", "output", "runner_cut", "shown_cut", "killed"),
    [
        (0, "done", False, False, False),
        (3, "boom", False, False, False),
        (None, "partial", False, False, True),
        (0, "tail", True, True, False),
        (0, "x" * MESSAGE_TEXT_LIMIT + "END", False, True, False),
    ],
)
def test_admin_result_status_reaches_notice_and_expert_turn(
    tmp_path: Path,
    *,
    returncode: int | None,
    output: str,
    runner_cut: bool,
    shown_cut: bool,
    killed: bool,
) -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(conversation.id, 10, "run it", "reason", NOW)
    assert journal.decide_admin(
        op, 11, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW - timedelta(minutes=1)
    )

    class Lock:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()

        def hold(self) -> asyncio.Lock:
            return self.lock

    class Runner:
        async def run_admin(
            self,
            argv: Sequence[str],
            *,
            timeout_s: float,
            cwd: Path,
            max_bytes: int,
            secrets: Sequence[str],
        ) -> AdminCommandResult:
            del argv, timeout_s, cwd, max_bytes, secrets
            return AdminCommandResult(returncode=returncode, output=output, cut=runner_cut)

    turns: list[int] = []

    async def run(_conversation_id: int, turn: int) -> None:
        turns.append(turn)

    executor = AdminExecutor(  # noqa: S604 -- fake runner
        journal,
        harness.conversations,
        Runner(),
        Lock(),
        harness.outbox,
        Clock(),
        directory=tmp_path,
        shell="/bin/sh",
        timeout_s=1,
        max_bytes=100,
        secrets=(),
        rooms=FakeRooms(CHAT),
        max_text=MESSAGE_TEXT_LIMIT,
    )
    asyncio.run(executor.execute(op, run))
    assert len(turns) == 1
    turn = harness.conversations.turn(turns[0])
    assert turn is not None
    message = harness.database.connection.execute(
        "SELECT body FROM messages WHERE notice_key = ?", (f"admin:result:{op}",)
    ).fetchone()
    assert message is not None
    texts = (turn.text, json.loads(message["body"])["text"])
    for text in texts:
        assert len(text) <= MESSAGE_TEXT_LIMIT
        status, _, body = text.partition("\n")
        assert output.endswith(body)
        assert re.search(r"exit code", status, re.IGNORECASE)
        if returncode is None:
            assert re.search(r"unavailable", status, re.IGNORECASE)
        else:
            assert re.search(rf"\b{returncode}\b", status)
        assert bool(re.search(r"timeout|killed", status, re.IGNORECASE)) == killed
        if shown_cut:
            assert re.search(r"\b(cut|trimmed|truncated)\b", status, re.IGNORECASE)
        else:
            assert re.search(
                r"\b(all|complete|entire|full|whole|uncut|untrimmed|not cut|no cut)\b",
                status,
                re.IGNORECASE,
            )


def test_admin_restart_notices_are_not_replayed() -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    ops: list[int] = []
    for update in (10, 20):
        op, _ = journal.intend_admin(conversation.id, update, "echo test", "reason", NOW)
        assert journal.decide_admin(
            op,
            update + 1,
            str(ADMIN),
            admin=True,
            approve=True,
            now=NOW,
            cutoff=NOW - timedelta(minutes=1),
        )
        ops.append(op)
    assert journal.issue_admin(ops[0], NOW) is not None
    channel = FakeChannel()
    sender = OutboxSender(harness.outbox, channel, Clock(), interval_s=1)
    for _ in range(2):
        recover_admin(
            journal,
            harness.conversations,
            Clock(),
            harness.outbox,
            rooms=FakeRooms(CHAT),
        )
    assert harness.outbox.notice_queued(f"admin:interrupted:{ops[0]}")
    assert harness.outbox.notice_queued(f"admin:interrupted:{ops[1]}")
    asyncio.run(sender.tick())
    assert len(channel.sent) == 2
    issued = journal.admin_op(ops[0])
    approved = journal.admin_op(ops[1])
    assert issued is not None
    assert approved is not None
    assert issued[1] == "unknown"
    assert approved[1] == "void"


class _DownOnce(FakeChannel):
    """Fails the first send as a transient platform outage does, then accepts every send."""

    async def send(self, place: Place, message: Message) -> str:
        if not self.attempts:
            self.attempts.append((place, message))
            raise ChannelError
        return await super().send(place, message)


class _CrashingOutbox(JournalOutbox):
    """Crashes the first time a notice is queued, as a crash inside the settlement would."""

    def __init__(self, database: Database) -> None:
        super().__init__(database, MessageStore(database, Reporter()))
        self.crashes = 1

    def queue(
        self,
        number: int | None,
        purpose: Purpose,
        places: Sequence[Place],
        message: Message,
        *,
        key: str,
        now: datetime,
        slot: str | None = None,
        standing: bool = False,
        conversation_id: int | None = None,
    ) -> None:
        if self.crashes:
            self.crashes -= 1
            raise CrashError
        super().queue(
            number,
            purpose,
            places,
            message,
            key=key,
            now=now,
            slot=slot,
            standing=standing,
            conversation_id=conversation_id,
        )


def _issued_admin_op() -> tuple[Harness, JournalProposals, int, int]:
    """Return a harness whose admin conversation has one issued command, and that command."""
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(conversation.id, 10, "echo test", "reason", NOW)
    assert journal.decide_admin(
        op, 11, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW - timedelta(minutes=1)
    )
    assert journal.issue_admin(op, NOW) is not None
    return harness, journal, op, conversation.id


def _admin_state(journal: JournalProposals, op: int) -> str:
    """Return the state of an admin command, which the journal must hold."""
    stored = journal.admin_op(op)
    assert stored is not None
    return stored[1]


def _interrupted_notice_rows(database: Database, op: int) -> list[int | None]:
    """Return the conversation of each journaled notice of this interrupted command."""
    rows = database.connection.execute(
        "SELECT conversation_id FROM messages WHERE notice_key = ?", (f"admin:interrupted:{op}",)
    ).fetchall()
    return [row["conversation_id"] for row in rows]


def test_an_interrupted_admin_notice_whose_delivery_fails_is_delivered_once_after_a_restart() -> (
    None
):
    harness, journal, op, conversation_id = _issued_admin_op()
    recover_admin(journal, harness.conversations, Clock(), harness.outbox, rooms=FakeRooms(CHAT))
    channel = _DownOnce()
    asyncio.run(OutboxSender(harness.outbox, channel, Clock(), interval_s=1).tick())
    assert channel.sent == []

    # keep: the notice must survive a process that holds none of the objects of the first run
    database = harness.database
    outbox = JournalOutbox(database, MessageStore(database, Reporter()))
    recover_admin(
        JournalProposals(database, OpStore(database), channel_name=CHANNEL),
        Conversations(database),
        Clock(),
        outbox,
        rooms=FakeRooms(CHAT),
    )
    asyncio.run(OutboxSender(outbox, channel, Clock(), interval_s=1).tick())
    asyncio.run(OutboxSender(outbox, channel, Clock(), interval_s=1).tick())

    [(place, message)] = channel.sent
    assert place == telegram_place(CHAT, THREAD)
    assert message.text == "Command is unknown after restart. Please check the host."
    assert _interrupted_notice_rows(database, op) == [conversation_id]
    assert _admin_state(journal, op) == "unknown"
    assert journal.issue_admin(op, NOW) is None


def test_a_crash_while_queueing_the_notice_leaves_the_command_to_be_settled_again() -> None:
    harness, journal, op, _ = _issued_admin_op()
    with pytest.raises(CrashError):
        recover_admin(
            journal,
            harness.conversations,
            Clock(),
            _CrashingOutbox(harness.database),
            rooms=FakeRooms(CHAT),
        )
    assert _admin_state(journal, op) == "issued"
    assert _interrupted_notice_rows(harness.database, op) == []

    recover_admin(journal, harness.conversations, Clock(), harness.outbox, rooms=FakeRooms(CHAT))
    assert _admin_state(journal, op) == "unknown"
    assert len(_interrupted_notice_rows(harness.database, op)) == 1


@pytest.mark.parametrize("crash", [False, True])
def test_admin_output_is_redacted_before_journal_topic_and_expert(
    tmp_path: Path, *, crash: bool
) -> None:
    from shikiri_yaku.adapters.commands import ProcessCommandRunner  # noqa: PLC0415

    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(
        conversation.id, 10, "printf xx; printf SEC; printf RET; printf extra", "reason", NOW
    )
    assert journal.decide_admin(
        op, 11, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW - timedelta(minutes=1)
    )

    async def scenario() -> None:
        class Lock:
            def __init__(self) -> None:
                self.lock = asyncio.Lock()

            def hold(self) -> asyncio.Lock:
                return self.lock

        executor = AdminExecutor(  # noqa: S604 -- harmless printf through CommandRunner
            journal,
            harness.conversations,
            ProcessCommandRunner(),
            Lock(),
            harness.outbox,
            Clock(),
            directory=tmp_path,
            shell="/bin/sh",
            timeout_s=10,
            max_bytes=5,
            secrets=("SECRET",),
            rooms=FakeRooms(CHAT),
            max_text=MESSAGE_TEXT_LIMIT,
        )
        turns: list[int] = []

        async def run(_conversation_id: int, turn: int) -> None:
            turns.append(turn)
            if crash:
                raise CrashError

        if crash:
            with pytest.raises(CrashError):
                await executor.execute(op, run)
        else:
            await executor.execute(op, run)
        assert len(turns) == 1
        turn = harness.conversations.turn(turns[0])
        assert turn is not None
        assert turn.text.endswith("\nxx***")
        assert turn.state == "pending"
        assert harness.conversations.pending_admin_followups() == [
            (conversation.id, turn.update_id)
        ]
        message = harness.database.connection.execute(
            "SELECT body FROM messages WHERE notice_key = ?", (f"admin:result:{op}",)
        ).fetchone()
        assert json.loads(message["body"])["text"] == turn.text
        row = harness.database.connection.execute(
            "SELECT payload FROM ops WHERE id = ?", (op,)
        ).fetchone()
        assert "SECRET" not in row["payload"]
        assert "SECRET" not in message["body"]
        assert json.loads(row["payload"])["cut"] == 1

    asyncio.run(scenario())


def test_admin_result_and_output_notice_roll_back_together() -> None:
    harness = Harness({}, mode=MODE_ADMIN, admins=frozenset({str(ADMIN)}))
    harness.conversations.create(
        1, origin=telegram_place(CHAT), text="q", scope=[], now=NOW, mode=MODE_ADMIN
    )
    harness.conversations.opened(1, str(THREAD))
    conversation = harness.conversations.get(1)
    assert conversation is not None
    journal = JournalProposals(harness.database, OpStore(harness.database), channel_name=CHANNEL)
    op, _ = journal.intend_admin(conversation.id, 10, "echo safe", "reason", NOW)
    assert journal.decide_admin(
        op, 11, str(ADMIN), admin=True, approve=True, now=NOW, cutoff=NOW - timedelta(minutes=1)
    )
    assert journal.issue_admin(op, NOW) is not None

    def crash() -> None:
        from shikiri_yaku.domain.outbound import Purpose  # noqa: PLC0415

        harness.outbox.queue(
            None,
            Purpose.EXPERT,
            [telegram_place(CHAT, THREAD)],
            Message("safe"),
            key=f"admin:result:{op}",
            now=NOW,
            conversation_id=conversation.id,
        )
        raise CrashError

    with pytest.raises(CrashError):
        journal.finish_admin(op, NOW, "safe", 0, 1.0, cut=False, notice=crash)
    state = journal.admin_op(op)
    assert state is not None
    assert state[1] == "issued"
    assert harness.conversations.pending_admin_followups() == []
    assert (
        harness.database.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ?", (f"admin:result:{op}",)
        ).fetchone()[0]
        == 0
    )


def test_an_unmarked_task_is_submitted_as_before() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "first")])
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.requests == ["first"]
    assert alpha.modes == [None]
    assert "submitted as task T-first" in harness.final_body(FIRST)


def test_a_planning_task_is_submitted_in_plan_mode_to_execute_the_plan() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "rework the billing", True)])
    harness.tap(2, submit_all_data(FIRST))
    assert alpha.modes == ["plan"]
    assert len(alpha.requests) == 1
    request = alpha.requests[0]
    assert "rework the billing" in request
    assert "Plan this work" in request
    assert "execute the plan" in request
    assert "follow-up tasks" in request
    assert "dependencies" in request
    assert "planning-only" not in request
    assert "do not create" not in request.lower()
    assert "submitted as task" in harness.final_body(FIRST)


def test_planning_tasks_are_marked_in_the_task_list() -> None:
    harness = Harness({"alpha": FakeShikiru(), "beta": FakeShikiru()})
    harness.answer(FIRST, [("alpha", "small fix"), ("beta", "big migration", True)])
    shown = "\n".join(harness.texts(FIRST))
    assert "1. alpha · small fix" in shown
    assert "2. beta · big migration (planning)" in shown
    assert "(planning)" not in shown.split("2.")[0]


def test_a_submitted_plan_with_a_planning_task_survives_a_restart() -> None:
    alpha = FakeShikiru()
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "rework the billing", True)])
    harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    harness.tap(3, submit_all_data(FIRST))
    assert len(alpha.requests) == 1
    assert alpha.modes == ["plan"]
    assert "submitted as task" in harness.final_body(FIRST)


def test_a_crash_midway_through_a_plan_with_a_planning_task_recovers_without_resubmit() -> None:
    alpha = FakeShikiru(crash_on=1)
    harness = Harness({"alpha": alpha})
    harness.answer(FIRST, [("alpha", "rework the billing", True), ("alpha", "second")])
    with pytest.raises(CrashError):
        harness.tap(2, submit_all_data(FIRST))
    harness.proposals = harness.restart()
    harness.proposals.recover()
    harness.tap(3, submit_all_data(FIRST))
    assert len(alpha.requests) == 1
    assert alpha.modes == ["plan"]
    modes = [
        row[0]
        for row in harness.database.connection.execute(
            "SELECT json_extract(payload, '$.mode') FROM ops WHERE kind = 'submit' ORDER BY id"
        ).fetchall()
    ]
    assert modes == ["plan", None]
    final = harness.final_body(FIRST)
    assert (
        "1. alpha · rework the billing: unknown, it may have reached Shikiru;"
        " not sent again, please check" in final
    )
    assert "2. alpha · second: not submitted" in final
    assert "Interrupted by a restart" in final
    assert (
        harness.count(
            "SELECT COUNT(*) FROM messages WHERE notice_key = ?",
            f"expert:proposal:{FIRST}:interrupted",
        )
        == 1
    )
