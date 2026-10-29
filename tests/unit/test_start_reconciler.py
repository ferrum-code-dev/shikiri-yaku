"""Unsettled starts are settled by observing the engine, and never started again (§10)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.start_reconciler import StartReconciler
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.journal import Database, JournalStarts, NewSignal, OpStore, SignalStore
from shikiri_yaku.ports import AttemptState, EngineStatus, ShikiruLinkDownError

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import ShikiruClient

START: Final = datetime(2026, 10, 29, tzinfo=UTC)
CONFIRM_WITHIN: Final = timedelta(seconds=60)
SIGNAL: Final = Signal("api", Kind.ENGINE_DOWN, "status", "", "engine is down")
NEW_SIGNAL: Final = NewSignal("api:engine_down:status", "api", "engine_down", "", "engine is down")


LINK_DOWN: Final = "the link is down"


class StoppedError(Exception):
    """Ends the reconciler's loop at a sleep, once the clock has no sleeps left."""


class StartClient:
    """Shikiru as a recovery sees it: whether the engine is up, and how often `start` ran."""

    def __init__(self, *, engine_up: bool) -> None:
        self.engine_up = engine_up
        self.starts = 0
        self.link_down = False

    @property
    def linked(self) -> bool:
        return not self.link_down

    async def status(self) -> EngineStatus:
        if self.link_down:
            raise ShikiruLinkDownError(LINK_DOWN)
        return EngineStatus(engine_up=self.engine_up, quotas={})

    async def start_engine(self) -> None:
        self.starts += 1


class FakeClock:
    """Moves on by `step` at each sleep, and stops the run at a sleep once `sleeps` are used up."""

    def __init__(self, at: datetime, *, step: timedelta = timedelta(0), sleeps: int = 0) -> None:
        self.at = at
        self.step = step
        self.sleeps = sleeps

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        del seconds
        if self.sleeps == 0:
            raise StoppedError
        self.sleeps -= 1
        self.at += self.step


@pytest.fixture
def journal() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _starts(journal: Database) -> JournalStarts:
    return JournalStarts(journal, OpStore(journal), SignalStore(journal))


def _reconciler(journal: Database, client: StartClient, clock: FakeClock) -> StartReconciler:
    return StartReconciler(
        _starts(journal),
        {"api": cast("ShikiruClient", client)},
        clock,
        confirm_within=CONFIRM_WITHIN,
        interval_s=1,
    )


def _state(journal: Database) -> str | None:
    attempt = _starts(journal).existing(SIGNAL)
    return None if attempt is None else attempt.state


async def _start_then_crash(journal: Database, client: StartClient, *, command_ran: bool) -> None:
    """The first run journals a start, then dies; the command may or may not have run."""
    SignalStore(journal).insert_if_absent(NEW_SIGNAL, START)
    _starts(journal).issue(SIGNAL, START)
    if command_ran:
        await client.start_engine()


@pytest.mark.parametrize(
    ("command_ran", "engine_up", "elapsed", "state"),
    [
        (False, True, timedelta(0), AttemptState.CONFIRMED),
        (True, True, timedelta(0), AttemptState.CONFIRMED),
        (False, False, timedelta(0), AttemptState.ISSUED),
        (True, False, timedelta(0), AttemptState.ISSUED),
        (False, False, CONFIRM_WITHIN, AttemptState.FAILED),
        (True, False, CONFIRM_WITHIN, AttemptState.FAILED),
    ],
)
def test_an_unsettled_start_is_settled_by_the_engine_and_never_run_again(
    journal: Database,
    *,
    command_ran: bool,
    engine_up: bool,
    elapsed: timedelta,
    state: str,
) -> None:
    client = StartClient(engine_up=engine_up)
    asyncio.run(_start_then_crash(journal, client, command_ran=command_ran))
    starts_before_recovery = client.starts
    with pytest.raises(StoppedError):
        asyncio.run(_reconciler(journal, client, FakeClock(START + elapsed)).run())
    assert client.starts == starts_before_recovery
    assert _state(journal) == state


def test_the_gate_stays_shut_while_an_unsettled_start_is_pending(journal: Database) -> None:
    client = StartClient(engine_up=False)
    asyncio.run(_start_then_crash(journal, client, command_ran=False))
    reconciler = _reconciler(journal, client, FakeClock(START))
    with pytest.raises(StoppedError):
        asyncio.run(reconciler.run())
    assert not reconciler.settled.is_set()


def test_the_gate_waits_for_the_link_to_come_up_even_without_a_start(journal: Database) -> None:
    client = StartClient(engine_up=True)
    client.link_down = True
    reconciler = _reconciler(journal, client, FakeClock(START))
    with pytest.raises(StoppedError):
        asyncio.run(reconciler.run())
    assert not reconciler.settled.is_set()
    client.link_down = False
    with pytest.raises(StoppedError):
        asyncio.run(reconciler.run())
    assert reconciler.settled.is_set()


def test_the_gate_opens_once_the_unsettled_starts_are_settled(journal: Database) -> None:
    client = StartClient(engine_up=True)
    asyncio.run(_start_then_crash(journal, client, command_ran=True))
    reconciler = _reconciler(journal, client, FakeClock(START))
    with pytest.raises(StoppedError):
        asyncio.run(reconciler.run())
    assert reconciler.settled.is_set()


def test_a_link_that_stays_down_opens_the_gate_at_the_bound(journal: Database) -> None:
    client = StartClient(engine_up=True)
    client.link_down = True
    asyncio.run(_start_then_crash(journal, client, command_ran=True))
    clock = FakeClock(START, step=CONFIRM_WITHIN / 2, sleeps=2)
    reconciler = _reconciler(journal, client, clock)
    with pytest.raises(StoppedError):
        asyncio.run(reconciler.run())
    assert reconciler.settled.is_set()
    assert _state(journal) == AttemptState.ISSUED


def test_a_link_outage_leaves_the_start_for_the_next_tick(journal: Database) -> None:
    client = StartClient(engine_up=True)
    asyncio.run(_start_then_crash(journal, client, command_ran=True))
    reconciler = _reconciler(journal, client, FakeClock(START))
    uncertain = {SIGNAL.id}
    client.link_down = True
    asyncio.run(reconciler.tick(uncertain))
    assert _state(journal) == AttemptState.ISSUED
    client.link_down = False
    asyncio.run(reconciler.tick(uncertain))
    assert _state(journal) == AttemptState.CONFIRMED


def test_a_start_outside_the_recovery_snapshot_is_left_alone(journal: Database) -> None:
    client = StartClient(engine_up=False)
    asyncio.run(_start_then_crash(journal, client, command_ran=False))
    reconciler = _reconciler(journal, client, FakeClock(START + CONFIRM_WITHIN))
    asyncio.run(reconciler.tick(set()))
    assert _state(journal) == AttemptState.ISSUED
