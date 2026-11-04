"""A pull request that closes ends its review message on every copy; a late tap changes nothing.

The task is done (merged), the pull request is withdrawn, or Shikiru escalates
`pr_closed`. Each closes the review signal, its copies are edited to the final state, and its
reminders stop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

import pytest

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.event_source import (
    CLOSED_UNMERGED_LINE,
    MERGED_LINE,
    WITHDRAWN_LINE,
    EventSource,
)
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.signal import PullRequest
from shikiri_yaku.extensions.review.message import review_message
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalEvents,
    JournalFinalEdits,
    JournalSchedule,
    MessageStore,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.ports import ShikiruEvent

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import Channel, Message, ShikiruClient

PROJECT: Final = "api"
NOW: Final = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
PRODUCT: Final = "Shop"
PULL: Final = PullRequest("https://github.com/acme/api/pull/7", 3)
REFS: Final = ("1001/42", "1002/17")
CLOSING_ID: Final = 2


def _closing(kind: str, **data: Any) -> tuple[str, dict[str, Any]]:
    return kind, data


CLOSINGS = [
    pytest.param(_closing("task_done"), MERGED_LINE, id="merged"),
    pytest.param(_closing("pr_withdrawn"), WITHDRAWN_LINE, id="withdrawn"),
    pytest.param(
        _closing("escalation", escalation_id="E1", reason="pr_closed"),
        CLOSED_UNMERGED_LINE,
        id="closed unmerged",
    ),
]


def _event(kind: str, data: dict[str, Any]) -> ShikiruEvent:
    return ShikiruEvent(
        id=CLOSING_ID, ts=NOW, task_id="T1", kind=kind, message=f"{kind} message", data=data
    )


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Client:
    """Shikiru's log holds the one closing event; the escalation it names is not in the list."""

    def __init__(self, event: ShikiruEvent) -> None:
        self._event = event

    async def wait_for_events(self, after: int, wait_s: float) -> list[ShikiruEvent]:
        del wait_s
        return [self._event] if after < self._event.id else []

    async def get_escalation(self, escalation_id: str) -> None:
        del escalation_id


class _Channel:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Message]] = []

    async def edit(self, message_ref: str, message: Message) -> None:
        self.edits.append((message_ref, message))


@pytest.fixture
def database() -> Generator[Database]:
    memory = Database.open(":memory:")
    yield memory
    memory.close()


def _review_on_screen(database: Database) -> int:
    """Journal a pull request whose review message reached two chats, as the Router leaves it."""
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(
            "api:pr_review:1", PROJECT, "pr_review", "pr_opened", "x", "T1", pull_request=PULL
        ),
        NOW,
    ).row_id
    signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
    signals.transition(number, SignalState.HANDLING, SignalState.FORWARDED, NOW)
    stored = signals.get(number)
    assert stored is not None
    body = review_message(
        number, stored.to_signal(), PULL, "Add retries", product=PRODUCT, in_topic=False
    )
    messages = MessageStore(database, _Reporter())
    for index, ref in enumerate(REFS):
        row = messages.record_outbound(
            signal_number=number,
            channel="telegram",
            chat=str(1001 + index),
            purpose=Purpose.NOTICE,
            body=body,
            notice_key=f"review:{number}",
            now=NOW,
            standing=True,
        )
        messages.confirm_delivery(row.row_id, ref)
    return number


def _replies(database: Database) -> JournalReplies:
    return JournalReplies(
        database, SignalStore(database), OpStore(database), channel_name="telegram"
    )


def _close(database: Database, event: ShikiruEvent) -> None:
    signals = SignalStore(database)
    cursors = CursorStore(database, signals)
    cursors.record_batch(PROJECT, [], 1, None, NOW)
    journal = JournalEvents(database, cursors, OpStore(database))
    source = EventSource(PROJECT, cast("ShikiruClient", _Client(event)), journal, _Clock(), 1)
    asyncio.run(source.poll())


def _tick(database: Database, channel: _Channel) -> None:
    editor = CopyEditor(
        JournalFinalEdits(database, _replies(database)),
        cast("Channel", channel),
        _Clock(),
        interval_s=1,
    )
    asyncio.run(editor.tick())


@pytest.mark.parametrize(("closing", "line"), CLOSINGS)
def test_each_way_a_pull_request_closes_edits_every_copy_without_buttons(
    database: Database, closing: tuple[str, dict[str, Any]], line: str
) -> None:
    number = _review_on_screen(database)
    schedule = JournalSchedule(
        database,
        SignalStore(database),
        MessageStore(database, _Reporter()),
        OpStore(database),
    )
    assert [awaiting.number for awaiting in schedule.awaiting_answer()] == [number]

    _close(database, _event(*closing))
    channel = _Channel()
    _tick(database, channel)
    _tick(database, channel)

    assert [ref for ref, _ in channel.edits] == list(REFS)
    for _, message in channel.edits:
        assert message.buttons == ()
        assert message.text.endswith(line)
    assert schedule.awaiting_answer() == []
    stored = SignalStore(database).get(number)
    assert stored is not None
    assert stored.state is SignalState.CLOSED


@pytest.mark.parametrize(("closing", "line"), CLOSINGS)
def test_a_late_stop_reminders_tap_after_the_close_changes_nothing(
    database: Database, closing: tuple[str, dict[str, Any]], line: str
) -> None:
    number = _review_on_screen(database)
    _close(database, _event(*closing))
    channel = _Channel()
    _tick(database, channel)
    edits = len(channel.edits)

    _replies(database).stop_notice(number, NOW)
    _tick(database, channel)

    assert len(channel.edits) == edits
    assert all(message.text.endswith(line) for _, message in channel.edits)
    acks = database.connection.execute(
        "SELECT COUNT(*) AS acks FROM ops WHERE identity = ?", (f"ack:{number}",)
    ).fetchone()
    assert acks["acks"] == 0
    stored = SignalStore(database).get(number)
    assert stored is not None
    assert stored.state is SignalState.CLOSED
