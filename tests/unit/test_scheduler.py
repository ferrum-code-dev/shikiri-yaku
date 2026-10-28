from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.domain.schedule import due_occurrence
from shikiri_yaku.journal import (
    Database,
    JournalSchedule,
    MessageStore,
    NewSignal,
    OpStore,
    Purpose,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.schedule import Parked

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
FIRST_H = 4
REPEAT_H = 24
CHAT = "1001"


class _Clock:
    def __init__(self) -> None:
        self.moment = START

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Target:
    def __init__(self) -> None:
        self.woken: list[Parked] = []

    async def wake(self, parked: Parked) -> None:
        self.woken.append(parked)


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failing = False

    async def send(self, destination: str, text: str) -> str:
        if self.failing:
            raise ChannelError
        self.sent.append((destination, text))
        return str(len(self.sent))

    async def edit(self, message_ref: str, text: str) -> None:
        raise NotImplementedError


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = _Clock()
        self.target = _Target()
        self.channel = _Channel()
        self.signals = SignalStore(database)
        self.messages = MessageStore(database)

    def scheduler(self) -> Scheduler:
        """A new Scheduler over the same journal: what an Operator restart gives."""
        log = JournalSchedule(self.database, self.signals, self.messages, OpStore(self.database))
        return Scheduler(
            log,
            self.target,
            self.channel,
            self.clock,
            interval_s=1,
            reminder_first_h=FIRST_H,
            reminder_repeat_h=REPEAT_H,
        )

    def signal(self, name: str, state: SignalState, wake_at: datetime | None = None) -> int:
        number = self.signals.insert_if_absent(
            NewSignal(f"api:escalation:{name}", "api", "escalation", "agent_error", "x", "T-1"),
            START,
        ).row_id
        self.signals.classify(
            number, category="infra", classified_by="rule", verdict=None, handler="infra", now=START
        )
        self.signals.transition(number, SignalState.NEW, SignalState.HANDLING, START)
        self.signals.transition(number, SignalState.HANDLING, state, START, wake_at=wake_at)
        return number

    def ask(self, number: int) -> None:
        sent = self.messages.record_outbound(
            signal_number=number,
            channel="telegram",
            chat=CHAT,
            purpose=Purpose.QUESTION,
            notice_key=None,
            now=START,
        )
        self.messages.confirm_delivery(sent.row_id, "m1")


def _tick(world: _World) -> None:
    asyncio.run(world.scheduler().tick())


@pytest.fixture
def world(memory: Database) -> _World:
    return _World(memory)


def test_parked_signal_is_dispatched_once_when_due_also_after_a_restart(
    world: _World,
) -> None:
    number = world.signal("e1", SignalState.PARKED, START + timedelta(hours=1))
    _tick(world)
    assert world.target.woken == []

    world.clock.moment = START + timedelta(hours=1)
    _tick(world)
    _tick(world)

    assert [(woken.number, woken.handler) for woken in world.target.woken] == [(number, "infra")]
    stored = world.signals.get(number)
    assert stored is not None
    assert stored.state is SignalState.HANDLING


@pytest.mark.parametrize(
    ("hours", "occurrence"),
    [(3, 0), (4, 1), (27, 1), (28, 2), (51, 2), (52, 3), (200, 9)],
)
def test_reminder_occurrence_table(hours: int, occurrence: int) -> None:
    now = START + timedelta(hours=hours)
    assert due_occurrence(START, now, FIRST_H, REPEAT_H) == occurrence


def test_reminders_fire_at_the_configured_times_and_never_twice(world: _World) -> None:
    world.ask(world.signal("e1", SignalState.FORWARDED))
    for hours, sent in [(3, 0), (4, 1), (5, 1), (27, 1), (28, 2), (28, 2)]:
        world.clock.moment = START + timedelta(hours=hours)
        _tick(world)
        assert len(world.channel.sent) == sent, hours
    assert {destination for destination, _ in world.channel.sent} == {CHAT}


def test_answered_signal_gets_no_reminder(world: _World) -> None:
    world.ask(world.signal("e1", SignalState.ANSWERED))
    world.clock.moment = START + timedelta(hours=FIRST_H)
    _tick(world)
    assert world.channel.sent == []


def test_a_failed_send_stays_booked_in_the_outbox_and_is_not_rescheduled(
    world: _World,
) -> None:
    number = world.signal("e1", SignalState.FORWARDED)
    world.ask(number)
    world.clock.moment = START + timedelta(hours=FIRST_H)
    world.channel.failing = True
    _tick(world)
    world.channel.failing = False
    _tick(world)

    assert world.channel.sent == []
    assert len(MessageStore(world.database).unconfirmed()) == 1
