"""Every copy of a resolved question is edited to its final state, journaled before the edit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest
from pydantic import TypeAdapter

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.bootstrap import STANDING_KINDS
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalEvents,
    JournalFinalEdits,
    MessageStore,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    Ack,
    Button,
    ChannelError,
    ChannelRefusedError,
    Message,
    ResolvePayload,
    ShikiruEvent,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import ShikiruClient

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
RESOLVED_AT: Final = datetime(2026, 10, 28, 12, 3, tzinfo=UTC)
REFS: Final = ("1001/42", "1002/17")
_BODY: Final = TypeAdapter(Message)
QUESTION: Final = Message("S1 · question", (Button("Keep", "a:1:1"), Button("Drop", "a:1:2")))


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Channel:
    """Records each edit with the final body the journal held when the platform was asked."""

    def __init__(self, database: Database, *, failures: dict[str, Exception] | None = None) -> None:
        self._database = database
        self.failures = failures or {}
        self.edits: list[tuple[str, Message, bool]] = []

    async def send(self, destination: Destination, message: Message) -> str:
        raise NotImplementedError

    async def edit(self, message_ref: str, message: Message) -> None:
        row = self._database.connection.execute(
            "SELECT final_body FROM messages WHERE platform_message_id = ?", (message_ref,)
        ).fetchone()
        self.edits.append((message_ref, message, row["final_body"] is not None))
        if message_ref in self.failures:
            raise self.failures.pop(message_ref)

    async def delete(self, message_ref: str) -> None:
        raise NotImplementedError


class _Harness:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                "api:escalation:E-1",
                "api",
                "escalation",
                "decision_required",
                "question",
                "T-1",
                "E-1",
            ),
            NOW,
        ).row_id
        messages = MessageStore(self.database, _Reporter())
        for index, ref in enumerate(REFS):
            row = messages.record_outbound(
                signal_number=self.number,
                channel="telegram",
                chat=str(1001 + index),
                purpose=Purpose.QUESTION,
                body=QUESTION,
                notice_key=None,
                now=NOW,
            )
            messages.confirm_delivery(row.row_id, ref)
        self.replies = JournalReplies(
            self.database, self.signals, OpStore(self.database), standing_kinds=STANDING_KINDS
        )

    def resolve(self) -> None:
        payload = ResolvePayload(
            number=self.number,
            project="api",
            task_id="T-1",
            reason="decision_required",
            escalation_id="E-1",
            kind="answer",
            text="Keep",
            label="Keep",
        )
        op_id = self.replies.intend_resolve(700, payload, NOW)
        assert op_id is not None
        self.replies.claim_resolve(op_id, NOW)
        self.replies.acknowledge_resolve(
            op_id, Ack(id=12, status="queued", result=None, message=None), NOW
        )
        OpStore(self.database).confirm_command("api", "12", RESOLVED_AT)

    def resolve_unconfirmed(self) -> None:
        self.resolve()
        self.database.connection.execute("UPDATE ops SET state = 'issued'")

    def editor(self, channel: _Channel) -> CopyEditor:
        return CopyEditor(
            JournalFinalEdits(self.database, self.replies), channel, _Clock(), interval_s=1
        )

    def edited(self, ref: str) -> bool:
        row = self.database.connection.execute(
            "SELECT final_edited FROM messages WHERE platform_message_id = ?", (ref,)
        ).fetchone()
        return bool(row["final_edited"])

    def body(self, ref: str) -> Message:
        origin = self.replies.origin("telegram", ref)
        assert origin is not None
        return origin.message


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


@pytest.fixture
def h() -> Generator[_Harness]:
    harness = _Harness()
    yield harness
    harness.database.close()


def test_resolution_edits_every_copy_without_buttons_after_journaling_it(h: _Harness) -> None:
    h.resolve()
    channel = _Channel(h.database)
    editor = h.editor(channel)
    asyncio.run(editor.tick())
    asyncio.run(editor.tick())
    assert [(ref, journaled) for ref, _, journaled in channel.edits] == [
        (ref, True) for ref in REFS
    ]
    for ref, message, _ in channel.edits:
        assert message.buttons == ()
        assert message.text.startswith(QUESTION.text)
        assert "Keep at 12:03 UTC" in message.text
        assert h.edited(ref)
        assert h.body(ref) == QUESTION


def test_a_question_nobody_resolved_keeps_its_buttons(h: _Harness) -> None:
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    assert channel.edits == []
    assert h.body(REFS[0]) == QUESTION


def test_a_failed_edit_is_finished_by_a_later_tick_or_a_restart(h: _Harness) -> None:
    h.resolve()
    channel = _Channel(h.database, failures={REFS[0]: ChannelError("down")})
    asyncio.run(h.editor(channel).tick())
    assert not h.edited(REFS[0])
    assert h.edited(REFS[1])
    restarted = _Channel(h.database)
    asyncio.run(h.editor(restarted).tick())
    assert [ref for ref, _, _ in restarted.edits] == [REFS[0]]
    assert h.edited(REFS[0])


def test_an_edit_the_platform_refuses_is_not_repeated(h: _Harness) -> None:
    h.resolve()
    channel = _Channel(h.database, failures={REFS[0]: ChannelRefusedError("gone")})
    editor = h.editor(channel)
    asyncio.run(editor.tick())
    asyncio.run(editor.tick())
    assert [ref for ref, _, _ in channel.edits].count(REFS[0]) == 1


def test_a_long_question_is_cut_so_its_final_state_still_fits(h: _Harness) -> None:
    long = Message("Q" * MESSAGE_TEXT_LIMIT, QUESTION.buttons, heading=QUESTION.text)
    h.database.connection.execute("UPDATE messages SET body = ?", (_BODY.dump_json(long).decode(),))
    h.resolve()
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    assert len(channel.edits) == len(REFS)
    for _, message, _ in channel.edits:
        assert len(message.shown()) <= MESSAGE_TEXT_LIMIT
        assert message.heading == QUESTION.text
        assert "Resolved: Keep" in message.text


class _Events:
    """A Shikiru whose event log holds one event."""

    def __init__(self, event: ShikiruEvent) -> None:
        self._event = event

    async def wait_for_events(self, after: int, wait_s: float) -> list[ShikiruEvent]:  # noqa: ARG002
        return [self._event] if after < self._event.id else []


def _observe(h: _Harness, **data: object) -> None:
    event = ShikiruEvent(
        id=2, ts=NOW, task_id="T-1", kind="escalation_resolved", message="", data=data
    )
    cursors = CursorStore(h.database, h.signals)
    cursors.record_batch("api", [], 1, None, NOW)
    journal = JournalEvents(h.database, cursors, OpStore(h.database))
    source = EventSource("api", cast("ShikiruClient", _Events(event)), journal, _Clock(), 1)
    asyncio.run(source.poll())


def test_a_resolve_made_elsewhere_edits_every_copy_and_closes_the_signal(h: _Harness) -> None:
    _observe(h, escalation_id="E-1", resolution="retry")
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    assert [ref for ref, _, _ in channel.edits] == list(REFS)
    for ref, message, journaled in channel.edits:
        assert journaled
        assert message.buttons == ()
        assert message.text == f"{QUESTION.text}\n\nResolved in Shikiru: retry"
        assert h.edited(ref)
    stored = h.signals.get(h.number)
    assert stored is not None
    assert stored.state == "closed"


def test_a_resolve_the_operator_issued_is_not_shown_as_made_elsewhere(h: _Harness) -> None:
    h.resolve_unconfirmed()
    _observe(h, escalation_id="E-1", resolution="answer", command_id=12)
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    assert all(
        "elsewhere" not in m.text and "in Shikiru" not in m.text for _, m, _ in channel.edits
    )
    assert all("Keep at" in m.text for _, m, _ in channel.edits)


def test_a_copy_sent_after_the_external_resolve_is_edited_too(h: _Harness) -> None:
    late = MessageStore(h.database, _Reporter()).record_outbound(
        signal_number=h.number,
        channel="telegram",
        chat="1003",
        purpose=Purpose.QUESTION,
        body=QUESTION,
        notice_key=None,
        now=NOW,
    )
    _observe(h, escalation_id="E-1", resolution="retry", command_id=999)
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    MessageStore(h.database, _Reporter()).confirm_delivery(late.row_id, "1003/18")
    asyncio.run(h.editor(channel).tick())
    assert [ref for ref, _, _ in channel.edits] == [*REFS, "1003/18"]
    assert all(message.buttons == () for _, message, _ in channel.edits)


def test_the_late_event_of_an_op_marked_failed_is_not_shown_as_made_elsewhere(
    h: _Harness,
) -> None:
    h.resolve_unconfirmed()
    h.database.connection.execute("UPDATE ops SET state = 'failed'")
    _observe(h, escalation_id="E-1", resolution="answer", command_id=12)
    channel = _Channel(h.database)
    asyncio.run(h.editor(channel).tick())
    assert channel.edits == []
