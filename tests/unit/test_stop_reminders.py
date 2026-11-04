"""A Stop reminders tap on an engine-down message ends its reminders and its message.

The tap removes the reminders and the message reads "Reminders stopped"; the
signal stays open until the engine is up again, and then the message reads "Engine is up again".
No reminder is sent in between, even when the clock is past the time one would be due.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.bootstrap import schedule_log
from shikiri_yaku.domain.outbound import ENGINE_UP_LINE, REMINDERS_STOPPED_LINE, Purpose
from shikiri_yaku.domain.signal import Closing, Kind
from shikiri_yaku.extensions.product.message import information_message
from shikiri_yaku.journal import (
    Database,
    JournalFinalEdits,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.schedule import Parked
    from shikiri_yaku.ports import Channel, Message

PROJECT: Final = "api"
CHAT: Final = "1001"
REF: Final = "1001/42"
NOW: Final = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
FIRST_H: Final = 4
REMINDER_DUE: Final = NOW + timedelta(hours=FIRST_H)
LATER: Final = NOW + timedelta(days=2)


class _Clock:
    def __init__(self) -> None:
        self.moment = NOW

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Target:
    async def wake(self, parked: Parked) -> None:
        pytest.fail(f"a parked signal was woken: {parked}")


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


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


def _engine_down_on_screen(database: Database) -> int:
    """Journal a standing engine-down message that reached the PO, as the Router leaves it."""
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(f"{PROJECT}:engine_down:1", PROJECT, Kind.ENGINE_DOWN, "agent_error", "x", "T1"),
        NOW,
    ).row_id
    signals.classify(
        number, category="infra", classified_by="rule", verdict=None, handler="infra", now=NOW
    )
    signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
    signals.transition(number, SignalState.HANDLING, SignalState.FORWARDED, NOW)
    stored = signals.get(number)
    assert stored is not None
    body = information_message(
        number,
        stored.to_signal(),
        title="The engine is down",
        in_topic=False,
        note="Shikiru could not restart it.",
        suggestion="Start it on the host.",
        standing=True,
    )
    messages = MessageStore(database, _Reporter())
    row = messages.record_outbound(
        signal_number=number,
        channel="telegram",
        chat=CHAT,
        purpose=Purpose.NOTICE,
        body=body,
        notice_key=f"forward:{number}",
        now=NOW,
        standing=True,
    )
    messages.confirm_delivery(row.row_id, REF)
    return number


def _replies(database: Database) -> JournalReplies:
    return JournalReplies(
        database, SignalStore(database), OpStore(database), channel_name="telegram"
    )


def _remind(database: Database, clock: _Clock) -> list[str]:
    """Run the Scheduler at the clock's moment; return the chats of every reminder queued so far."""
    scheduler = Scheduler(
        schedule_log(database, _Reporter()),
        _Target(),
        clock,
        interval_s=1,
        reminder_first_h=FIRST_H,
        reminder_repeat_h=24,
    )
    asyncio.run(scheduler.tick())
    outbox = JournalOutbox(database, MessageStore(database, _Reporter()))
    return [pending.destination.chat for pending in outbox.pending()]


def _edit_copies(database: Database, clock: _Clock, channel: _Channel) -> None:
    editor = CopyEditor(
        JournalFinalEdits(database, _replies(database)),
        cast("Channel", channel),
        clock,
        interval_s=1,
    )
    asyncio.run(editor.tick())


def test_a_tap_ends_the_reminders_and_the_engine_return_ends_the_message(
    database: Database,
) -> None:
    number = _engine_down_on_screen(database)
    clock = _Clock()
    channel = _Channel()

    _replies(database).stop_notice(number, NOW)
    clock.moment = REMINDER_DUE
    assert _remind(database, clock) == []
    _edit_copies(database, clock, channel)
    assert [ref for ref, _ in channel.edits] == [REF]
    assert channel.edits[0][1].text.endswith(REMINDERS_STOPPED_LINE)
    assert channel.edits[0][1].buttons == ()

    SignalStore(database).close(
        Closing(PROJECT, Kind.ENGINE_DOWN, forwarded_only=True, final_line=ENGINE_UP_LINE),
        REMINDER_DUE,
    )
    clock.moment = LATER
    assert _remind(database, clock) == []
    _edit_copies(database, clock, channel)
    assert [ref for ref, _ in channel.edits] == [REF, REF]
    assert channel.edits[1][1].text.endswith(ENGINE_UP_LINE)
    assert channel.edits[1][1].buttons == ()
    stored = SignalStore(database).get(number)
    assert stored is not None
    assert stored.state is SignalState.CLOSED


def test_without_a_tap_the_engine_down_message_is_reminded_at_the_reminder_time(
    database: Database,
) -> None:
    _engine_down_on_screen(database)
    clock = _Clock()
    clock.moment = REMINDER_DUE
    assert _remind(database, clock) == [CHAT]
