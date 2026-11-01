from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.install_executor import ANSWER, FINAL_LINE, InstallExecutor
from shikiri_yaku.app.install_reask import InstallReasker
from shikiri_yaku.app.install_reconciler import InstallReconciler
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Lifecycle, LifecycleState, Signal
from shikiri_yaku.extensions.infrastructure.install import InstallApprovalPresenter
from shikiri_yaku.journal import (
    Database,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.install_reconcile import JournalInstallReconcile
from shikiri_yaku.journal.install_runs import JournalInstallRuns
from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.journal.signals import SignalState
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    CommandError,
    CommandResult,
    EngineStatus,
    EscalationRecord,
    EscalationStatus,
    EscalationView,
    ResolutionKind,
    ShikiruLinkDownError,
    ShikiruRejectedError,
)
from tests.support.destinations import FixedDestinations
from tests.support.notices import FailingReporter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.ports import ShikiruClient

NOW: Final = datetime(2026, 10, 31, 12, tzinfo=UTC)
STARTED: Final = datetime(2026, 10, 31, 9, tzinfo=UTC)
RESTART: Final = NOW + timedelta(hours=1)
FOUND: Final = "/opt/homebrew/bin/ffmpeg"
SHELL: Final = "/bin/sh"
OWNER: Final = Destination("telegram", "1001")
LOST_MIN: Final = 2
DONE: Final = Ack(id=7, status=AckStatus.DONE, result=None, message=None)
QUEUED: Final = Ack(id=8, status=AckStatus.QUEUED, result=None, message=None)
SIGNAL = Signal(
    "api",
    Kind.ESCALATION,
    "E-1",
    "decision_required",
    "the build needs ffmpeg",
    task_id="T-1",
    escalation_id="E-1",
)
PAYLOAD = InstallOpPayload(
    number=1,
    escalation_id="E-1",
    tool="ffmpeg",
    manager="brew",
    package="ffmpeg",
    why="not on the host PATH",
    argv=("brew", "install", "ffmpeg"),
)


def _view(status: str, resolution: str | None = None) -> EscalationView:
    record = EscalationRecord(
        id="E-1",
        task_id="T-1",
        reason="decision_required",
        headline="the build needs ffmpeg",
        status=status,
        resolution=resolution,
    )
    return EscalationView(
        escalation=record, packet=None, question="which?", agent_options=[], resolutions=[]
    )


class _Clock:
    """The time of the restart, an hour after the install was made."""

    def now(self) -> datetime:
        return RESTART

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Shikiru:
    """One escalation as Shikiru reports it; an answer is refused with `error` when one is given."""

    def __init__(
        self,
        *,
        view: EscalationView | None = None,
        error: Exception | None = None,
        engine_up: bool = True,
    ) -> None:
        self._view = _view(EscalationStatus.OPEN) if view is None else view
        self._error = error
        self._engine_up = engine_up
        self.answers: list[tuple[str, str, str | None]] = []

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        del escalation_id
        return self._view

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        self.answers.append((escalation_id, kind, text))
        if self._error is not None:
            raise self._error
        return DONE

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=self._engine_up, quotas={})


class _Check:
    """Finds the tool at `found`, or reports it missing when `found` is None; `error` stops it."""

    def __init__(self, found: str | None = FOUND, *, error: CommandError | None = None) -> None:
        self.found = found
        self.error = error

    async def find(self, tool: str) -> str | None:
        del tool
        if self.error is not None:
            raise self.error
        return self.found


class _ChangingCheck(_Check):
    """The check first misses after a failing run, then finds the tool at restart."""

    def __init__(self) -> None:
        super().__init__(None)
        self.calls = 0

    async def find(self, tool: str) -> str | None:
        del tool
        self.calls += 1
        return None if self.calls == 1 else FOUND


class _Host:
    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        yield


class _Commands:
    def __init__(self, returncode: int = 1) -> None:
        self._returncode = returncode

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del argv, timeout_s, cwd, env
        return CommandResult(self._returncode, "partial install", "manager exited late")


class _InterruptedCommand:
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del argv, timeout_s, cwd, env
        raise asyncio.CancelledError


class _Files:
    def __init__(self, scratch: Path) -> None:
        self._scratch = scratch

    def scratch_dir(self) -> Path:
        return self._scratch

    def save_output(self, op_id: int, text: str) -> str:
        del op_id, text
        return "install-output"


class _InterruptedForwarder:
    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        del number, signal, note, suggestion
        raise asyncio.CancelledError


class _Runner:
    """Records the ops started, and the ops a run of this process has in hand."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.in_hand: set[int] = set()

    def start(self, op_id: int) -> None:
        self.started.append(op_id)

    def running(self, op_id: int) -> bool:
        return op_id in self.in_hand


class _Reshower:
    def __init__(self) -> None:
        self.shown: list[str] = []

    async def reshow(self, number: int, signal: Signal, note: str, *, key: str) -> bool:
        del number, signal, note
        self.shown.append(key)
        return True


class _Lifecycles:
    """The engine's last lifecycle event: a start at `STARTED`, or none when `present` is False."""

    def __init__(self, *, present: bool = True) -> None:
        self._present = present

    def state(self, project: str) -> LifecycleState | None:
        del project
        if not self._present:
            return None
        return LifecycleState(1, Lifecycle(1, "engine_started", has_reason=False, at=STARTED), None)

    def respect_stop(self, project: str, event_id: int) -> None:
        del project, event_id
        raise NotImplementedError


class _World:
    """One forwarded escalation, with the reconciler over the journal and fakes for the outside."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.signals = SignalStore(database)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                SIGNAL.id,
                SIGNAL.project,
                SIGNAL.kind.value,
                SIGNAL.reason,
                SIGNAL.text,
                SIGNAL.task_id,
                SIGNAL.escalation_id,
            ),
            NOW,
        ).row_id
        self.signals.transition(self.number, SignalState.NEW, SignalState.FORWARDED, NOW)
        self.ops = OpStore(database)
        self.messages = MessageStore(database, FailingReporter())
        self.outbox = JournalOutbox(database, self.messages)
        self.shikiru = _Shikiru()
        self.check = _Check()
        self.runner = _Runner()
        self.reshower = _Reshower()
        self.lifecycles = _Lifecycles()

    def install(self, *, issued: bool, ack: Ack | None = None) -> int:
        payload = PAYLOAD.model_copy(update={"number": self.number})
        operation = self.ops.intend_install(f"install:{SIGNAL.id}", SIGNAL, payload, NOW)
        assert self.ops.transition(operation.id, OpState.INTENDED, OpState.APPROVED, NOW)
        if issued:
            assert self.ops.transition(operation.id, OpState.APPROVED, OpState.ISSUED, NOW)
        if ack is not None:
            self.ops.acknowledge_install(operation.id, ack, None, NOW)
        return operation.id

    def reconcile(self) -> None:
        clock = _Clock()
        reasker = InstallReasker(
            self.ops,
            InstallApprovalPresenter(),
            self.outbox,
            FixedDestinations([OWNER]),
            clock,
        )
        reconciler = InstallReconciler(
            JournalInstallReconcile(self.database, self.signals, self.ops),
            self.lifecycles,
            {"api": cast("ShikiruClient", self.shikiru)},
            self.runner,
            reshower=self.reshower,
            check=self.check,
            reasker=reasker,
            clock=clock,
            lost_command_min=LOST_MIN,
        )
        asyncio.run(reconciler.run())

    def state(self, op_id: int) -> str:
        stored = self.ops.get(op_id)
        assert stored is not None
        return stored.state

    def acknowledgement(self, op_id: int) -> Ack | None:
        stored = self.ops.get(op_id)
        assert stored is not None
        return stored.acknowledgement

    def signal_state(self) -> str:
        stored = self.signals.get(self.number)
        assert stored is not None
        return stored.state


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def world(memory: Database) -> _World:
    return _World(memory)


def test_an_approved_install_no_run_has_claimed_is_started(world: _World) -> None:
    op_id = world.install(issued=False)

    world.reconcile()

    assert world.runner.started == [op_id]
    assert world.state(op_id) == OpState.APPROVED


def test_an_approved_install_a_run_has_in_hand_is_not_started_again(world: _World) -> None:
    op_id = world.install(issued=False)
    world.runner.in_hand.add(op_id)

    world.reconcile()

    assert world.runner.started == []


def test_an_issued_install_whose_tool_is_found_is_answered_once(world: _World) -> None:
    op_id = world.install(issued=True)

    world.reconcile()
    world.reconcile()

    [(_, kind, text)] = world.shikiru.answers
    assert kind == ResolutionKind.ANSWER
    assert text == ANSWER.format(tool="ffmpeg", manager="brew", path=FOUND)
    assert world.state(op_id) == OpState.ISSUED
    assert world.acknowledgement(op_id) == DONE
    assert world.runner.started == []


def test_a_nonzero_run_interrupted_during_forward_is_checked_and_answered_at_restart(
    world: _World, tmp_path: Path
) -> None:
    op_id = world.install(issued=False)
    check = _ChangingCheck()
    executor = InstallExecutor(
        JournalInstallRuns(world.database, world.signals, world.ops),
        {"api": cast("ShikiruClient", world.shikiru)},
        _InterruptedForwarder(),
        check,
        _Commands(),
        host=_Host(),
        files=_Files(tmp_path),
        clock=_Clock(),
        shell=SHELL,
        timeout_s=60,
        tail_lines=5,
        forward_retry_s=1,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(executor.execute(op_id))
    assert world.state(op_id) == OpState.ISSUED
    assert check.calls == 1

    world.check = _Check()
    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert world.runner.started == []
    assert len(world.shikiru.answers) == 1
    row = world.database.connection.execute(
        "SELECT check_failed_at FROM ops WHERE id = ?", (op_id,)
    ).fetchone()
    assert row["check_failed_at"] is not None


def test_a_crash_after_claim_before_command_reasks_without_running(
    world: _World, tmp_path: Path
) -> None:
    op_id = world.install(issued=False)
    executor = InstallExecutor(
        JournalInstallRuns(world.database, world.signals, world.ops),
        {"api": cast("ShikiruClient", world.shikiru)},
        _InterruptedForwarder(),
        _Check(),
        _InterruptedCommand(),
        host=_Host(),
        files=_Files(tmp_path),
        clock=_Clock(),
        shell=SHELL,
        timeout_s=60,
        tail_lines=5,
        forward_retry_s=1,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(executor.execute(op_id))
    assert world.state(op_id) == OpState.ISSUED

    world.check = _Check(None)
    world.reconcile()

    assert world.state(op_id) == OpState.VOID
    assert world.runner.started == []
    assert world.ops.get_install(f"install:{SIGNAL.id}:after:{op_id}") is not None


def test_a_crash_after_run_before_answer_is_retried_by_the_restart_check(
    world: _World, tmp_path: Path
) -> None:
    world.shikiru = _Shikiru(error=ShikiruLinkDownError("the link is down"))
    op_id = world.install(issued=False)
    executor = InstallExecutor(
        JournalInstallRuns(world.database, world.signals, world.ops),
        {"api": cast("ShikiruClient", world.shikiru)},
        _InterruptedForwarder(),
        _Check(),
        _Commands(returncode=0),
        host=_Host(),
        files=_Files(tmp_path),
        clock=_Clock(),
        shell=SHELL,
        timeout_s=60,
        tail_lines=5,
        forward_retry_s=1,
    )

    asyncio.run(executor.execute(op_id))
    assert world.state(op_id) == OpState.ISSUED
    assert len(world.shikiru.answers) == 1

    world.shikiru = _Shikiru()
    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert len(world.shikiru.answers) == 1
    assert world.runner.started == []


def test_a_link_lost_after_the_install_answer_is_retried_from_the_check(world: _World) -> None:
    world.shikiru = _Shikiru(error=ShikiruLinkDownError("the link is down"))
    op_id = world.install(issued=True)

    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert len(world.shikiru.answers) == 1

    world.shikiru = _Shikiru()
    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert len(world.shikiru.answers) == 1
    assert world.runner.started == []


def test_an_issued_install_whose_tool_is_missing_is_asked_again(world: _World) -> None:
    world.check.found = None
    old_id = world.install(issued=True)

    world.reconcile()

    assert world.shikiru.answers == []
    assert world.state(old_id) == OpState.VOID
    new = world.ops.get_install(f"install:{SIGNAL.id}:after:{old_id}")
    assert new is not None
    assert new.payload == PAYLOAD.model_copy(update={"number": world.number})
    [stored] = world.messages.for_signal(world.number)
    assert stored.purpose == Purpose.APPROVAL


def test_an_answer_shikiru_refuses_voids_the_op_and_keeps_the_question(world: _World) -> None:
    world.shikiru = _Shikiru(error=ShikiruRejectedError("already resolved"))
    op_id = world.install(issued=True)

    world.reconcile()

    assert world.state(op_id) == OpState.VOID
    assert world.signal_state() == SignalState.FORWARDED
    assert world.messages.for_signal(world.number) == []


def test_a_link_down_during_the_answer_leaves_the_op_for_the_next_start(world: _World) -> None:
    world.shikiru = _Shikiru(error=ShikiruLinkDownError("the link is down"))
    op_id = world.install(issued=True)

    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert world.acknowledgement(op_id) is None
    assert world.messages.for_signal(world.number) == []


def test_a_failing_check_leaves_the_op_for_the_next_start(world: _World) -> None:
    world.check.error = CommandError("the check did not run")
    op_id = world.install(issued=True)

    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert world.acknowledgement(op_id) is None
    assert world.messages.for_signal(world.number) == []


@pytest.mark.parametrize(
    ("ack", "resolution", "expected"),
    [
        (None, ResolutionKind.ANSWER, OpState.CONFIRMED),
        (DONE, ResolutionKind.ANSWER, OpState.CONFIRMED),
        (None, ResolutionKind.CANCEL, OpState.VOID),
        (DONE, ResolutionKind.CANCEL, OpState.VOID),
    ],
    ids=["answered-unacked", "answered-acked", "cancelled-unacked", "cancelled-acked"],
)
def test_an_issued_install_whose_escalation_closed_is_settled_by_its_resolution(
    world: _World, ack: Ack | None, resolution: str, expected: str
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, resolution))
    op_id = world.install(issued=True, ack=ack)

    world.reconcile()

    assert world.state(op_id) == expected
    assert world.shikiru.answers == []


def test_a_queued_answer_still_open_after_the_engine_came_back_is_shown_again(
    world: _World,
) -> None:
    op_id = world.install(issued=True, ack=QUEUED)

    world.reconcile()

    assert world.state(op_id) == OpState.FAILED
    assert world.reshower.shown == [f"reshow:{op_id}"]


def test_a_queued_answer_is_not_lost_while_the_engine_is_down(world: _World) -> None:
    world.shikiru = _Shikiru(engine_up=False)
    op_id = world.install(issued=True, ack=QUEUED)

    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert world.reshower.shown == []


def test_a_queued_answer_is_not_lost_when_no_lifecycle_is_recorded(world: _World) -> None:
    world.lifecycles = _Lifecycles(present=False)
    op_id = world.install(issued=True, ack=QUEUED)

    world.reconcile()

    assert world.state(op_id) == OpState.ISSUED
    assert world.reshower.shown == []


def test_an_issued_install_a_run_has_in_hand_is_left_alone(world: _World) -> None:
    op_id = world.install(issued=True)
    world.runner.in_hand.add(op_id)

    world.reconcile()

    assert world.shikiru.answers == []
    assert world.state(op_id) == OpState.ISSUED


def _final_line(database: Database, number: int) -> str | None:
    row = database.connection.execute(
        "SELECT final_line FROM signals WHERE number = ?", (number,)
    ).fetchone()
    return None if row is None else row["final_line"]


def test_the_final_line_of_an_answered_install_is_set(memory: Database, world: _World) -> None:
    world.install(issued=True)

    world.reconcile()

    assert _final_line(memory, world.number) == FINAL_LINE.format(tool="ffmpeg", manager="brew")
