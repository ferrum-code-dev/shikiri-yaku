"""The Restart action starts a down engine once; a crash mid-restart never starts it again."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.start_reconciler import StartReconciler
from shikiri_yaku.domain.outcome import Forward, Resolved
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.restart import RestartFailedError
from shikiri_yaku.extensions.infrastructure.rules import ENGINE_ERROR
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalRetries,
    JournalStarts,
    JournalWaits,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    AttemptState,
    CommandError,
    DoctorCheck,
    EngineStatus,
    ShikiruProtocolError,
    ShikiruRejectedError,
    StartAttempt,
)
from tests.support.notices import journal_notifier, journal_outbox

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.ports import ShikiruClient

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CONFIRM_WITHIN = timedelta(seconds=60)
TASK = "T-1"
TMUX_DOWN = DoctorCheck(name="tmux", ok=False, detail="no session")
AGENT_CLI_DOWN = DoctorCheck(name="agent_cli", ok=False, detail="claude not on PATH")


class CrashError(Exception):
    """Ends a run where the process would die; the next run knows only what the journal holds."""


class StepClock:
    """Time moves only when the action sleeps, so the confirm window is exact."""

    def __init__(self) -> None:
        self.current = START

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class RestartClient:
    """Shikiru's engine as a restart sees it: a `start` brings it up when `starts_up` is set."""

    def __init__(self) -> None:
        self.engine_up = False
        self.starts_up = True
        self.failure: Exception | None = None
        self.doctor_error: Exception | None = None
        self.checks: list[DoctorCheck] = []
        self.starts = 0
        self.crash_before_command = False
        self.crash_after_command = False
        self.resolved: list[tuple[str, str]] = []

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=self.engine_up, quotas={})

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        del text
        self.resolved.append((escalation_id, kind))
        return Ack(id=1, status=AckStatus.DONE, result=None, message=None)

    async def start_engine(self) -> None:
        if self.crash_before_command:
            raise CrashError
        self.starts += 1
        if self.failure is not None:
            raise self.failure
        if self.starts_up:
            self.engine_up = True
        if self.crash_after_command:
            raise CrashError

    async def doctor(self) -> list[DoctorCheck]:
        if self.doctor_error is not None:
            raise self.doctor_error
        return self.checks


class LostNotice:
    """A notifier whose process dies before the notice reaches the outbox."""

    def tell(self, signal: Signal, text: str, key: str) -> None:
        del signal, text, key
        raise CrashError


class World:
    """One process's services over one journal; a second World on the same journal is a restart."""

    def __init__(
        self,
        database: Database,
        client: RestartClient,
        clock: StepClock,
        *,
        lose_notice: bool = False,
    ) -> None:
        ops = OpStore(database)
        self.starts = JournalStarts(database, ops, SignalStore(database))
        self.outbox = journal_outbox(database)
        guard = LoopGuard(JournalActions(ops), clock, 1, 1)
        notifier = LostNotice() if lose_notice else journal_notifier(self.outbox, clock)
        extension = infrastructure_extension(
            {"api": cast("ShikiruClient", client)},
            guard,
            JournalRetries(database, ops),
            clock,
            waits=JournalWaits(database, ops),
            starts=self.starts,
            notifier=notifier,
            quota_wait_max=timedelta(hours=24),
            quota_wait_margin=timedelta(minutes=1),
            restart_confirm=CONFIRM_WITHIN,
            restart_poll=timedelta(seconds=5),
        )
        self.handler = extension.handler

    def handle(self, signal: Signal) -> Outcome:
        return asyncio.run(self.handler.handle(signal))


def recover(database: Database, client: RestartClient, clock: StepClock) -> None:
    """Settle the starts an earlier run left unsettled, as a new process does before its signals."""
    starts = JournalStarts(database, OpStore(database), SignalStore(database))
    reconciler = StartReconciler(
        starts,
        {"api": cast("ShikiruClient", client)},
        clock,
        confirm_within=CONFIRM_WITHIN,
        interval_s=5,
    )
    uncertain = {start.signal.id for start in starts.unsettled()}
    asyncio.run(reconciler.tick(uncertain))


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


@pytest.fixture
def client() -> RestartClient:
    return RestartClient()


@pytest.fixture
def world(database: Database, client: RestartClient, clock: StepClock) -> World:
    return World(database, client, clock)


def engine_signal(kind: Kind, source: str) -> Signal:
    return Signal("api", kind, source, "", "the engine is down")


def journal_escalation(database: Database, reason: str, source: str) -> Signal:
    """Journal the escalation as the Router would, so a retry can find its number."""
    signal = Signal("api", Kind.ESCALATION, source, reason, "blocked", TASK, source)
    new = NewSignal(signal.id, "api", "escalation", reason, "blocked", TASK, source)
    SignalStore(database).insert_if_absent(new, START)
    return signal


@pytest.mark.parametrize("kind", [Kind.ENGINE_DOWN, Kind.ENGINE_FAULT])
def test_a_down_engine_is_started_once_confirmed_and_announced_once(
    world: World, client: RestartClient, kind: Kind
) -> None:
    signal = engine_signal(kind, "S1")

    assert isinstance(world.handle(signal), Resolved)
    assert isinstance(world.handle(signal), Resolved)

    assert client.starts == 1
    assert len(world.outbox.pending()) == 1
    assert world.starts.existing(signal) == StartAttempt(AttemptState.CONFIRMED)


@pytest.mark.parametrize(
    ("failure", "state"),
    [
        (ShikiruRejectedError("no tmux"), AttemptState.FAILED),
        (CommandError("did not finish"), AttemptState.ISSUED),
    ],
)
def test_a_start_that_fails_keeps_its_outcome_and_raises_the_failed_checks(
    world: World, client: RestartClient, failure: Exception, state: str
) -> None:
    signal = engine_signal(Kind.ENGINE_DOWN, "S1")
    client.failure = failure
    client.checks = [TMUX_DOWN]

    with pytest.raises(RestartFailedError) as caught:
        world.handle(signal)

    assert str(failure) in str(caught.value)
    assert "tmux" in str(caught.value)
    assert world.starts.existing(signal) == StartAttempt(state)
    assert client.starts == 1


def test_a_doctor_error_after_a_refused_start_crosses_unchanged(
    world: World, client: RestartClient
) -> None:
    client.failure = ShikiruRejectedError("no tmux")
    client.doctor_error = ShikiruProtocolError("no report")

    with pytest.raises(ShikiruProtocolError):
        world.handle(engine_signal(Kind.ENGINE_DOWN, "S1"))


def test_an_engine_that_stays_down_forwards_with_the_checks_after_the_window(
    world: World, client: RestartClient, clock: StepClock
) -> None:
    signal = engine_signal(Kind.ENGINE_FAULT, "S1")
    client.starts_up = False
    client.checks = [TMUX_DOWN]

    outcome = world.handle(signal)

    assert isinstance(outcome, Forward)
    assert "tmux" in outcome.note
    assert clock.current - START >= CONFIRM_WITHIN
    assert world.starts.existing(signal) == StartAttempt(AttemptState.FAILED)
    assert client.starts == 1


def test_a_second_restart_within_the_hour_is_refused_with_the_failed_checks(
    world: World, client: RestartClient
) -> None:
    assert isinstance(world.handle(engine_signal(Kind.ENGINE_DOWN, "S1")), Resolved)
    client.engine_up = False
    client.starts_up = False
    client.checks = [AGENT_CLI_DOWN]

    outcome = world.handle(engine_signal(Kind.ENGINE_DOWN, "S2"))

    assert isinstance(outcome, Forward)
    assert "agent_cli" in outcome.note
    assert client.starts == 1


def test_a_signal_whose_start_failed_earlier_is_never_started_again(
    world: World, client: RestartClient
) -> None:
    signal = engine_signal(Kind.ENGINE_DOWN, "S1")
    world.starts.issue(signal, START)
    world.starts.fail(signal, START)
    client.checks = [TMUX_DOWN]

    outcome = world.handle(signal)

    assert isinstance(outcome, Forward)
    assert "tmux" in outcome.note
    assert client.starts == 0


@pytest.mark.parametrize(
    ("command_ran", "engine_up", "outcome"),
    [
        (False, True, Resolved),
        (False, False, Forward),
        (True, True, Resolved),
        (True, False, Forward),
    ],
)
def test_a_crash_mid_restart_is_settled_by_the_engine_and_never_started_again(
    database: Database,
    client: RestartClient,
    clock: StepClock,
    *,
    command_ran: bool,
    engine_up: bool,
    outcome: type[Outcome],
) -> None:
    signal = engine_signal(Kind.ENGINE_DOWN, "S1")
    client.starts_up = engine_up
    client.engine_up = engine_up
    client.crash_before_command = not command_ran
    client.crash_after_command = command_ran
    first = World(database, client, clock)

    with pytest.raises(CrashError):
        first.handle(signal)
    assert first.starts.existing(signal) == StartAttempt(AttemptState.ISSUED)

    client.crash_before_command = False
    client.crash_after_command = False
    recover(database, client, clock)
    second = World(database, client, clock)

    assert isinstance(second.handle(signal), outcome)
    assert client.starts == (1 if command_ran else 0)
    assert len(second.outbox.pending()) == (1 if outcome is Resolved else 0)


@pytest.mark.parametrize("command_ran", [False, True])
def test_an_unconfirmed_start_still_spends_the_restart_budget(
    database: Database, client: RestartClient, clock: StepClock, *, command_ran: bool
) -> None:
    client.starts_up = False
    client.crash_before_command = not command_ran
    client.crash_after_command = command_ran
    with pytest.raises(CrashError):
        World(database, client, clock).handle(engine_signal(Kind.ENGINE_DOWN, "S1"))

    client.crash_before_command = False
    client.crash_after_command = False
    recover(database, client, clock)
    client.checks = [AGENT_CLI_DOWN]
    outcome = World(database, client, clock).handle(engine_signal(Kind.ENGINE_DOWN, "S2"))

    assert isinstance(outcome, Forward)
    assert "agent_cli" in outcome.note
    assert client.starts == (1 if command_ran else 0)


def test_an_engine_error_with_the_engine_up_is_only_given_back_to_shikiru(
    world: World, client: RestartClient, database: Database
) -> None:
    signal = journal_escalation(database, ENGINE_ERROR, "E1")
    client.engine_up = True

    assert isinstance(world.handle(signal), Resolved)

    assert client.starts == 0
    assert client.resolved == [("E1", "retry")]


def test_an_engine_error_with_the_engine_down_restarts_then_is_given_back(
    world: World, client: RestartClient, database: Database
) -> None:
    signal = journal_escalation(database, ENGINE_ERROR, "E1")

    assert isinstance(world.handle(signal), Resolved)

    assert client.starts == 1
    assert client.resolved == [("E1", "retry")]


@pytest.mark.parametrize(
    ("crash_after_command", "lose_notice"),
    [(True, False), (False, True)],
    ids=["after_command", "before_notice"],
)
def test_an_engine_error_crash_after_the_start_still_announces_the_restart_once(
    database: Database,
    client: RestartClient,
    clock: StepClock,
    *,
    crash_after_command: bool,
    lose_notice: bool,
) -> None:
    signal = journal_escalation(database, ENGINE_ERROR, "E1")
    client.crash_after_command = crash_after_command
    with pytest.raises(CrashError):
        World(database, client, clock, lose_notice=lose_notice).handle(signal)

    client.crash_after_command = False
    recover(database, client, clock)
    second = World(database, client, clock)

    assert isinstance(second.handle(signal), Resolved)
    assert isinstance(second.handle(signal), Resolved)
    assert client.starts == 1
    assert len(second.outbox.pending()) == 1
    assert client.resolved == [("E1", "retry")]


def test_an_engine_error_whose_restart_forwards_is_left_to_the_po(
    world: World, client: RestartClient, database: Database
) -> None:
    signal = journal_escalation(database, ENGINE_ERROR, "E1")
    client.starts_up = False
    client.checks = [TMUX_DOWN]

    outcome = world.handle(signal)

    assert isinstance(outcome, Forward)
    assert "tmux" in outcome.note
    assert client.resolved == []
