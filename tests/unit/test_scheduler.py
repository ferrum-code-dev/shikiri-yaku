from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.bootstrap import schedule_log
from shikiri_yaku.domain.schedule import due_occurrence
from shikiri_yaku.journal import (
    Database,
    Inserted,
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

    def scheduler(self, log: JournalSchedule | None = None) -> Scheduler:
        """A new Scheduler over the same journal: what an Operator restart gives."""
        return Scheduler(
            schedule_log(self.database) if log is None else log,
            self.target,
            self.channel,
            self.clock,
            interval_s=1,
            reminder_first_h=FIRST_H,
            reminder_repeat_h=REPEAT_H,
        )

    def signal(
        self,
        name: str,
        state: SignalState,
        wake_at: datetime | None = None,
        kind: str = "escalation",
    ) -> int:
        number = self.signals.insert_if_absent(
            NewSignal(f"api:{kind}:{name}", "api", kind, "agent_error", "x", "T-1"), START
        ).row_id
        self.signals.classify(
            number, category="infra", classified_by="rule", verdict=None, handler="infra", now=START
        )
        self.signals.transition(number, SignalState.NEW, SignalState.HANDLING, START)
        self.signals.transition(number, SignalState.HANDLING, state, START, wake_at=wake_at)
        return number

    def ask(
        self,
        number: int,
        chat: str = CHAT,
        purpose: Purpose = Purpose.QUESTION,
        key: str | None = None,
    ) -> None:
        sent = self.messages.record_outbound(
            signal_number=number,
            channel="telegram",
            chat=chat,
            purpose=purpose,
            notice_key=key,
            now=START,
        )
        self.messages.confirm_delivery(sent.row_id, f"m{sent.row_id}")


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


def test_repeated_copies_in_one_chat_get_one_reminder_and_each_owner_chat_gets_one(
    world: _World,
) -> None:
    number = world.signal("e1", SignalState.FORWARDED)
    copy = world.messages.record_outbound(
        signal_number=number,
        channel="telegram",
        chat=CHAT,
        purpose=Purpose.QUESTION,
        notice_key="copy",
        now=START,
    )
    world.messages.confirm_delivery(copy.row_id, "m-copy")
    world.ask(number)
    world.ask(number, chat="1002")
    world.clock.moment = START + timedelta(hours=FIRST_H)
    _tick(world)
    _tick(world)

    assert sorted(destination for destination, _ in world.channel.sent) == [CHAT, "1002"]


@pytest.mark.parametrize("final_state", [SignalState.ANSWERED, SignalState.CLOSED])
def test_unacknowledged_review_is_reminded_until_acknowledged_or_closed(
    world: _World,
    final_state: SignalState,
) -> None:
    number = world.signal("e1", SignalState.FORWARDED, kind="pr_review")
    world.ask(number, purpose=Purpose.NOTICE, key="review:api:T-1")
    world.clock.moment = START + timedelta(hours=FIRST_H)
    _tick(world)
    world.clock.moment = START + timedelta(hours=FIRST_H + 1)
    _tick(world)
    assert len(world.channel.sent) == 1

    world.signals.transition(number, SignalState.FORWARDED, final_state, world.clock.now())
    world.clock.moment = START + timedelta(hours=FIRST_H + REPEAT_H)
    _tick(world)
    assert len(world.channel.sent) == 1


@pytest.mark.parametrize("registered", [False, True])
def test_notice_reminders_follow_injected_registration(world: _World, *, registered: bool) -> None:
    kind = "another_notice_kind"
    number = world.signal("n1", SignalState.FORWARDED, kind=kind)
    world.ask(number, purpose=Purpose.NOTICE, key="original")
    world.ask(number, chat="1002", purpose=Purpose.NOTICE, key=f"remind:{number}:1")
    world.ask(world.signal("n2", SignalState.FORWARDED, kind="health"), purpose=Purpose.NOTICE)
    log = JournalSchedule(
        world.database,
        world.signals,
        world.messages,
        OpStore(world.database),
        reminder_notice_kinds=frozenset({kind}) if registered else frozenset(),
    )
    world.clock.moment = START + timedelta(hours=FIRST_H)
    asyncio.run(world.scheduler(log).tick())
    asyncio.run(world.scheduler(log).tick())

    assert [chat for chat, _ in world.channel.sent] == ([CHAT] if registered else [])


def test_an_interrupted_booking_leaves_nothing_and_the_retry_queues_every_owner(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    number = world.signal("e1", SignalState.FORWARDED)
    world.ask(number)
    world.ask(number, chat="1002")
    world.clock.moment = START + timedelta(hours=FIRST_H)
    record = MessageStore.record_outbound
    calls: list[str] = []

    def interrupted(store: MessageStore, **kwargs: Any) -> Inserted:
        calls.append(kwargs["chat"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return record(store, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(MessageStore, "record_outbound", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _tick(world)
    assert world.database.connection.execute("SELECT count(*) FROM ops").fetchone()[0] == 0
    assert world.messages.unconfirmed() == []

    _tick(world)
    assert sorted(destination for destination, _ in world.channel.sent) == [CHAT, "1002"]
