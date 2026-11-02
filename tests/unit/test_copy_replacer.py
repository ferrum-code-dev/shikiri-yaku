"""A replaced copy is deleted, and the attempt is journaled before the platform is asked."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import TypeAdapter

from shikiri_yaku.app.copy_replacer import CopyReplacer
from shikiri_yaku.bootstrap import STANDING_KINDS
from shikiri_yaku.domain.outbound import REPLACED_LINE, Purpose
from shikiri_yaku.journal import (
    Database,
    Delivery,
    JournalFinalEdits,
    JournalReplacements,
    MessageStore,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal
from shikiri_yaku.ports import Button, ChannelError, ChannelRefusedError, Message

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import FinalEdit

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
SLOT: Final = "question"
OLDER: Final = "1001/42"
NEWER: Final = "1001/43"
QUESTION: Final = Message("S1 · question", (Button("Keep", "a:1:1"),))
_BODY: Final = TypeAdapter(Message)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Channel:
    """Records each deletion with the journal's state of the copy when the platform is asked.

    Each failure is raised by one call, in order; the calls after them succeed.
    """

    def __init__(self, database: Database, *, failures: tuple[Exception, ...] = ()) -> None:
        self._database = database
        self._failures = list(failures)
        self.deleted: list[str] = []
        self.states: list[str] = []

    async def send(self, destination: Destination, message: Message) -> str:
        raise NotImplementedError

    async def edit(self, message_ref: str, message: Message) -> None:
        raise NotImplementedError

    async def delete(self, message_ref: str) -> None:
        self.deleted.append(message_ref)
        self.states.append(self._state(message_ref))
        if self._failures:
            raise self._failures.pop(0)

    def _state(self, message_ref: str) -> str:
        row = self._database.connection.execute(
            "SELECT delivery FROM messages WHERE platform_message_id = ?", (message_ref,)
        ).fetchone()
        return str(row["delivery"])


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Journal:
    """Two signals, and copies recorded and sent on them."""

    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.messages = MessageStore(self.database, _Reporter())
        self.replacements = JournalReplacements(self.database, self.messages)
        self.numbers = {index: self._signal(index) for index in (1, 2)}

    def _signal(self, index: int) -> int:
        new = NewSignal(
            f"api:escalation:E-{index}",
            "api",
            "escalation",
            "decision_required",
            "question",
            "T-1",
            f"E-{index}",
        )
        return self.signals.insert_if_absent(new, NOW).row_id

    def send(
        self,
        ref: str,
        *,
        signal: int = 1,
        chat: str = "1001",
        thread: int | None = None,
        slot: str | None = SLOT,
        sent: bool = True,
    ) -> None:
        row = self.messages.record_outbound(
            signal_number=self.numbers[signal],
            channel="telegram",
            chat=chat,
            thread=thread,
            purpose=Purpose.QUESTION,
            body=QUESTION,
            notice_key=f"question:{ref}",
            now=NOW,
            slot=slot,
        )
        if sent:
            self.messages.confirm_delivery(row.row_id, ref)

    def finish(self, ref: str) -> None:
        self.database.connection.execute(
            "UPDATE messages SET final_body = ? WHERE platform_message_id = ?",
            (_BODY.dump_json(QUESTION).decode(), ref),
        )

    def state(self, ref: str) -> str:
        row = self.database.connection.execute(
            "SELECT delivery FROM messages WHERE platform_message_id = ?", (ref,)
        ).fetchone()
        return str(row["delivery"])

    def to_edit(self) -> list[FinalEdit]:
        replies = JournalReplies(
            self.database, self.signals, OpStore(self.database), standing_kinds=STANDING_KINDS
        )
        return JournalFinalEdits(self.database, replies).to_edit()

    def notices(self) -> int:
        row = self.database.connection.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE purpose = ?", (Purpose.NOTICE,)
        ).fetchone()
        return int(row["n"])

    def replacer(self, channel: _Channel) -> CopyReplacer:
        return CopyReplacer(self.replacements, channel, _Clock(), interval_s=1)


@pytest.fixture
def journal() -> Generator[_Journal]:
    harness = _Journal()
    yield harness
    harness.database.close()


def _tick(replacer: CopyReplacer) -> None:
    asyncio.run(replacer.tick())


@pytest.mark.parametrize(
    ("slot", "signal", "chat", "thread", "sent", "replaced"),
    [
        pytest.param(SLOT, 1, "1001", None, True, True, id="same slot, signal, chat and topic"),
        pytest.param(None, 1, "1001", None, True, False, id="a newer copy without a slot"),
        pytest.param("install", 1, "1001", None, True, False, id="another slot"),
        pytest.param(SLOT, 2, "1001", None, True, False, id="another signal"),
        pytest.param(SLOT, 1, "1002", None, True, False, id="another chat"),
        pytest.param(SLOT, 1, "1001", 7, True, False, id="another topic"),
        pytest.param(SLOT, 1, "1001", None, False, False, id="a newer copy not sent yet"),
    ],
)
def test_a_copy_is_replaced_only_by_a_sent_newer_copy_of_its_own_slot(
    journal: _Journal,
    *,
    slot: str | None,
    signal: int,
    chat: str,
    thread: int | None,
    sent: bool,
    replaced: bool,
) -> None:
    journal.send(OLDER)
    journal.send(NEWER, signal=signal, chat=chat, thread=thread, slot=slot, sent=sent)

    assert journal.replacements.superseded() == ([OLDER] if replaced else [])


def test_a_copy_whose_final_state_is_journaled_is_left_to_the_copy_editor(
    journal: _Journal,
) -> None:
    journal.send(OLDER)
    journal.send(NEWER)
    journal.finish(OLDER)

    assert journal.replacements.superseded() == []


def test_the_newest_copy_of_a_slot_is_never_deleted(journal: _Journal) -> None:
    journal.send(OLDER)
    channel = _Channel(journal.database)

    _tick(journal.replacer(channel))

    assert channel.deleted == []


def test_a_superseded_copy_is_deleted_with_its_attempt_journaled_before_the_call(
    journal: _Journal,
) -> None:
    journal.send(OLDER)
    journal.send(NEWER)
    channel = _Channel(journal.database)

    _tick(journal.replacer(channel))

    assert channel.deleted == [OLDER]
    assert channel.states == [Delivery.DELETE_ATTEMPTED]
    assert journal.state(OLDER) == Delivery.DELETED
    assert journal.state(NEWER) == Delivery.SENT


def test_a_copy_the_platform_will_not_delete_is_kept_as_replaced_and_not_asked_again(
    journal: _Journal,
) -> None:
    journal.send(OLDER)
    journal.send(NEWER)
    channel = _Channel(journal.database, failures=(ChannelRefusedError("older than 48 hours"),))
    replacer = journal.replacer(channel)

    _tick(replacer)
    _tick(replacer)

    assert channel.deleted == [OLDER]
    assert journal.state(OLDER) == Delivery.SENT
    assert journal.notices() == 0
    [edit] = journal.to_edit()
    assert edit.message_ref == OLDER
    assert edit.message.buttons == ()
    assert edit.message.text == f"{QUESTION.text}\n\n{REPLACED_LINE}"


def test_a_transient_failure_is_tried_again_by_the_next_tick(journal: _Journal) -> None:
    journal.send(OLDER)
    journal.send(NEWER)
    channel = _Channel(journal.database, failures=(ChannelError("offline"),))
    replacer = journal.replacer(channel)

    _tick(replacer)
    assert journal.state(OLDER) == Delivery.SENT
    _tick(replacer)

    assert channel.deleted == [OLDER, OLDER]
    assert journal.state(OLDER) == Delivery.DELETED


def test_an_attempt_cut_short_by_a_crash_is_not_repeated(journal: _Journal) -> None:
    journal.send(OLDER)
    journal.send(NEWER)
    journal.replacements.begin_deletion(OLDER)
    channel = _Channel(journal.database)

    _tick(journal.replacer(channel))

    assert channel.deleted == []
    assert journal.state(OLDER) == Delivery.DELETE_ATTEMPTED


def test_undo_leaves_alone_a_copy_whose_body_a_secret_deletion_dropped(journal: _Journal) -> None:
    journal.send(OLDER, slot=None)
    journal.messages.record_deletion(OLDER, "a secret", now=NOW)

    journal.replacements.undo_deletion(OLDER)

    assert journal.state(OLDER) == Delivery.DELETE_ATTEMPTED
