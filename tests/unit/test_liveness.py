from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.liveness_source import LivenessSource
from shikiri_yaku.domain.signal import Kind, Lifecycle
from shikiri_yaku.journal import CursorStore, Database, JournalSources, SignalState, SignalStore
from shikiri_yaku.ports import EngineStatus, EventKinds, ShikiruLinkDownError

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.app.event_source import EventSource
    from shikiri_yaku.ports import ShikiruClient

PROJECT = "api"
BEFORE_RUN = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
RUN_START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
GRACE_MIN = 2
UNREACHABLE_MIN = 10
LAST_EVENT = 40
STOP_ID = 38
STARTED = EventKinds.ENGINE_STARTED
STOPPED = EventKinds.ENGINE_STOPPED


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        del seconds


class _NoEvents:
    async def poll(self, *, wait_s: float | None = None) -> bool:
        del wait_s
        return False


class _Engine:
    def __init__(self, *, up: bool) -> None:
        self.up = up
        self.down_link = False

    async def status(self) -> EngineStatus:
        if self.down_link:
            raise ShikiruLinkDownError
        return EngineStatus(engine_up=self.up, quotas={})


@dataclass
class _World:
    database: Database
    engine: _Engine
    clock: _Clock

    @property
    def journal(self) -> JournalSources:
        cursors = CursorStore(self.database, SignalStore(self.database))
        return JournalSources(cursors, SignalStore(self.database))

    def record(self, kind: str, at: datetime, *, reason: bool = False) -> None:
        lifecycle = Lifecycle(STOP_ID, kind, has_reason=reason, at=at)
        cursors = CursorStore(self.database, SignalStore(self.database))
        cursors.record_batch(PROJECT, [], LAST_EVENT, lifecycle, at)

    def operator(self) -> LivenessSource:
        """A new run of the Operator, started at the clock's current time."""
        journal = self.journal
        return LivenessSource(
            PROJECT,
            cast("ShikiruClient", self.engine),
            journal,
            self.clock,
            stop_grace_min=GRACE_MIN,
            events=cast("EventSource", _NoEvents()),
        )

    def health(self) -> HealthSource:
        return HealthSource(PROJECT, self.journal, self.clock, UNREACHABLE_MIN)

    def signals(self, kind: Kind) -> list[str]:
        rows = self.database.connection.execute(
            "SELECT id FROM signals WHERE kind = ? AND state != ?", (kind, SignalState.CLOSED)
        )
        return [row["id"] for row in rows]

    def respected(self) -> int | None:
        cursor = CursorStore(self.database, SignalStore(self.database)).get(PROJECT)
        return None if cursor is None else cursor.respected_stop_id


@pytest.fixture
def world() -> Generator[_World]:
    database = Database.open(":memory:")
    clock = _Clock(RUN_START)
    yield _World(database, _Engine(up=False), clock)
    database.close()


def _check(source: LivenessSource) -> None:
    asyncio.run(source.check())


def test_running_engine_raises_nothing(world: _World) -> None:
    world.engine.up = True
    world.record(STARTED, BEFORE_RUN)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == []


def test_engine_that_died_after_start_is_down(world: _World) -> None:
    world.record(STARTED, BEFORE_RUN)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == [f"api:engine_down:{LAST_EVENT}"]


def test_stop_with_a_reason_is_left_to_the_engine_fault_signal(world: _World) -> None:
    world.record(STOPPED, BEFORE_RUN, reason=True)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == []


def test_project_never_started_is_not_restarted(world: _World) -> None:
    world.database.connection.execute(
        "INSERT INTO cursors VALUES ('api', 5, NULL, NULL, NULL, NULL, NULL)"
    )
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == []


def test_clean_stop_before_this_run_is_down_at_once(world: _World) -> None:
    world.record(STOPPED, BEFORE_RUN)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == [f"api:engine_down:{LAST_EVENT}"]


def test_clean_stop_inside_the_grace_time_waits(world: _World) -> None:
    source = world.operator()
    world.record(STOPPED, RUN_START)
    world.clock.current = RUN_START + timedelta(minutes=GRACE_MIN) - timedelta(seconds=1)
    _check(source)
    assert (world.signals(Kind.ENGINE_DOWN), world.respected()) == ([], None)


def test_clean_stop_is_respected_after_the_grace_time(world: _World) -> None:
    source = world.operator()
    world.record(STOPPED, RUN_START)
    world.clock.current = RUN_START + timedelta(minutes=GRACE_MIN)
    _check(source)
    assert (world.signals(Kind.ENGINE_DOWN), world.respected()) == ([], STOP_ID)


def test_respected_stop_stays_respected_after_an_operator_restart(world: _World) -> None:
    first_run = world.operator()
    world.record(STOPPED, RUN_START)
    world.clock.current = RUN_START + timedelta(minutes=GRACE_MIN)
    _check(first_run)
    world.clock.current += timedelta(hours=1)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == []


def test_stop_inside_the_grace_time_before_a_restart_is_down_at_once(world: _World) -> None:
    first_run = world.operator()
    world.record(STOPPED, RUN_START)
    world.clock.current = RUN_START + timedelta(seconds=30)
    _check(first_run)
    world.clock.current += timedelta(minutes=5)
    _check(world.operator())
    assert world.signals(Kind.ENGINE_DOWN) == [f"api:engine_down:{LAST_EVENT}"]


def test_engine_down_id_is_the_same_across_restarts_and_checks(world: _World) -> None:
    world.record(STARTED, BEFORE_RUN)
    _check(world.operator())
    world.clock.current += timedelta(minutes=5)
    source = world.operator()
    _check(source)
    _check(source)
    assert world.signals(Kind.ENGINE_DOWN) == [f"api:engine_down:{LAST_EVENT}"]


def test_outage_opens_one_health_signal_after_the_patience_and_closes_on_recovery(
    world: _World,
) -> None:
    health = world.health()
    health.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN) - timedelta(seconds=1)
    health.link_failed()
    assert world.signals(Kind.HEALTH) == []
    world.clock.current += timedelta(seconds=1)
    health.link_failed()
    world.clock.current += timedelta(minutes=3)
    health.link_failed()
    assert world.signals(Kind.HEALTH) == ["api:health:link:2026-10-28T09:00Z"]
    health.link_recovered()
    assert world.signals(Kind.HEALTH) == []


def test_a_new_outage_is_a_new_signal(world: _World) -> None:
    health = world.health()
    health.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN)
    health.link_failed()
    health.link_recovered()
    world.clock.current += timedelta(hours=1)
    health.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN)
    health.link_failed()
    assert world.signals(Kind.HEALTH) == ["api:health:link:2026-10-28T10:10Z"]


def test_uninterrupted_outage_across_an_operator_restart_keeps_one_signal(
    world: _World,
) -> None:
    health = world.health()
    health.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN)
    health.link_failed()
    world.clock.current += timedelta(minutes=1)
    after_restart = world.health()
    after_restart.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN)
    after_restart.link_failed()
    assert world.signals(Kind.HEALTH) == ["api:health:link:2026-10-28T09:00Z"]


def test_first_recovery_after_a_start_closes_the_signal_of_an_earlier_run(world: _World) -> None:
    health = world.health()
    health.link_failed()
    world.clock.current += timedelta(minutes=UNREACHABLE_MIN)
    health.link_failed()
    after_restart = world.health()
    after_restart.link_recovered()
    assert world.signals(Kind.HEALTH) == []
