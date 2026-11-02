from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.install_executor import ANSWER, FINAL_LINE, REFUSED, InstallExecutor
from shikiri_yaku.app.install_reask import InstallReasker
from shikiri_yaku.app.install_reconciler import InstallReconciler
from shikiri_yaku.bootstrap import STANDING_KINDS
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Closing, Kind, Lifecycle, LifecycleState, Resolved, Signal
from shikiri_yaku.extensions.infrastructure.install import InstallApprovalPresenter
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalEvents,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.install_reconcile import JournalInstallReconcile
from shikiri_yaku.journal.install_runs import JournalInstallRuns
from shikiri_yaku.journal.messages import Delivery
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
    from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.ports import Clock, Reshower, ShikiruClient

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
TAMPERED_ARGV: Final = ("brew", "install", "ffmpeg", "--force")


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


class _LoopClock:
    def __init__(self, on_sleep: Callable[[], None]) -> None:
        self._now = NOW
        self._on_sleep = on_sleep

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._on_sleep()


class _StopRunError(Exception):
    pass


class _Shikiru:
    """One escalation as Shikiru reports it; an answer is refused with `error` when one is given."""

    def __init__(
        self,
        *,
        view: EscalationView | None = None,
        error: Exception | None = None,
        engine_up: bool = True,
        lookup_error_at: int | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self._view = _view(EscalationStatus.OPEN) if view is None else view
        self._error = error
        self._lookup_error_at = lookup_error_at
        self._lookup_error = lookup_error
        self.engine_up = engine_up
        self.answers: list[tuple[str, str, str | None]] = []
        self.lookups: list[str] = []

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        self.lookups.append(escalation_id)
        if len(self.lookups) == self._lookup_error_at and self._lookup_error is not None:
            raise self._lookup_error
        return self._view

    async def get_task(self, task_id: str) -> None:
        del task_id

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        self.answers.append((escalation_id, kind, text))
        if self._error is not None:
            raise self._error
        return DONE

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=self.engine_up, quotas={})

    def close(self, resolution: str) -> None:
        self._view = _view(EscalationStatus.RESOLVED, resolution)


class _Check:
    """Finds the tool at `found`, or reports it missing when `found` is None; `error` stops it."""

    def __init__(self, found: str | None = FOUND, *, error: CommandError | None = None) -> None:
        self.found = found
        self.error = error
        self.calls = 0

    async def find(self, tool: str) -> str | None:
        del tool
        self.calls += 1
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
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        del timeout_s, cwd, env
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
    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> bool:
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

    async def reshow(
        self,
        number: int,
        signal: Signal,
        note: str,
        *,
        key: str,
        even_if_closed: bool = False,
    ) -> bool:
        del number, signal, note, even_if_closed
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

    def reconciler(
        self,
        clock: Clock | None = None,
        *,
        interval_s: float = 1,
        clients: Mapping[str, ShikiruClient] | None = None,
        reshower: Reshower | None = None,
    ) -> InstallReconciler:
        clock = _Clock() if clock is None else clock
        reasker = InstallReasker(
            self.ops,
            InstallApprovalPresenter(),
            self.outbox,
            FixedDestinations([OWNER]),
            clock,
        )
        return InstallReconciler(
            JournalInstallReconcile(self.database, self.signals, self.ops),
            self.lifecycles,
            {"api": cast("ShikiruClient", self.shikiru)} if clients is None else clients,
            self.runner,
            reshower=self.reshower if reshower is None else reshower,
            check=self.check,
            reasker=reasker,
            clock=clock,
            lost_command_min=LOST_MIN,
            interval_s=interval_s,
        )

    def reconcile(self) -> None:
        asyncio.run(self.reconciler().tick())

    def tamper_argv(self, op_id: int, argv: tuple[str, ...]) -> None:
        """Rewrite the stored list the way someone with access to the journal could."""
        operation = self.ops.install_op(op_id)
        assert operation is not None
        payload = operation.payload.model_copy(update={"argv": argv})
        self.database.connection.execute(
            "UPDATE ops SET payload = ? WHERE id = ?", (payload.model_dump_json(), op_id)
        )

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


def _queue_approval(world: _World, op_id: int) -> None:
    operation = world.ops.install_op(op_id)
    assert operation is not None
    world.outbox.queue(
        world.number,
        Purpose.APPROVAL,
        [OWNER],
        InstallApprovalPresenter().render(op_id, operation.payload),
        key=f"install:{op_id}",
        now=NOW,
    )


def _product_forwarder(world: _World) -> ProductForwarder:
    return ProductForwarder(
        80,
        {"api": cast("ShikiruClient", world.shikiru)},
        world.outbox,
        _Clock(),
        FixedDestinations([OWNER]),
        button_label_max_chars=32,
        standing_kinds=STANDING_KINDS,
    )


def _interrupted_refusal(world: _World, tmp_path: Path, commands: _Commands) -> int:
    """An approved op whose stored list was changed, stopped while its refusal is forwarded."""
    op_id = world.install(issued=False)
    world.tamper_argv(op_id, TAMPERED_ARGV)
    executor = InstallExecutor(
        JournalInstallRuns(world.database, world.signals, world.ops),
        {"api": cast("ShikiruClient", world.shikiru)},
        _InterruptedForwarder(),
        _Check(),
        commands,
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
    return op_id


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


def test_approved_recovery_voids_an_escalation_resolved_before_local_event_sync(
    world: _World,
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, ResolutionKind.CANCEL))
    op_id = world.install(issued=False)

    world.reconcile()

    assert world.shikiru.lookups == ["E-1"]
    assert world.state(op_id) == OpState.VOID
    assert world.signal_state() == SignalState.FORWARDED
    assert world.runner.started == []


@pytest.mark.parametrize(
    "initial_state", [OpState.APPROVED, OpState.ISSUED], ids=["approved", "issued"]
)
def test_recovery_voids_ops_for_removed_components(world: _World, initial_state: str) -> None:
    op_id = world.install(issued=initial_state == OpState.ISSUED)
    world.signals.close_removed([], "component removed", RESTART)

    asyncio.run(world.reconciler(clients={}).tick())

    assert world.state(op_id) == OpState.VOID
    assert world.signal_state() == SignalState.CLOSED
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


@pytest.mark.parametrize("found", [FOUND, None], ids=["tool-found", "tool-missing"])
def test_a_refusal_interrupted_during_forward_fails_at_restart_without_answer_or_reask(
    world: _World, tmp_path: Path, found: str | None
) -> None:
    commands = _Commands()
    op_id = _interrupted_refusal(world, tmp_path, commands)
    check = _Check(found)
    world.check = check

    asyncio.run(world.reconciler(reshower=_product_forwarder(world)).tick())

    assert world.state(op_id) == OpState.FAILED
    assert commands.calls == []
    assert check.calls == 0
    assert world.shikiru.answers == []
    assert world.ops.get_install(f"install:{SIGNAL.id}:after:{op_id}") is None
    [question] = world.messages.for_signal(world.number)
    assert question.purpose == Purpose.QUESTION
    assert question.body is not None
    assert REFUSED in question.body.text


def test_a_refusal_the_link_cannot_deliver_stays_issued_until_a_later_pass(
    world: _World, tmp_path: Path
) -> None:
    world.shikiru = _Shikiru(
        lookup_error_at=2, lookup_error=ShikiruLinkDownError("reshow lookup failed")
    )
    commands = _Commands()
    op_id = _interrupted_refusal(world, tmp_path, commands)
    forwarder = _product_forwarder(world)

    asyncio.run(world.reconciler(reshower=forwarder).tick())

    assert world.state(op_id) == OpState.ISSUED
    assert world.messages.for_signal(world.number) == []

    asyncio.run(world.reconciler(reshower=forwarder).tick())

    assert world.state(op_id) == OpState.FAILED
    assert commands.calls == []
    assert world.shikiru.answers == []
    [question] = world.messages.for_signal(world.number)
    assert question.body is not None
    assert REFUSED in question.body.text


def test_an_answer_shikiru_refuses_voids_the_op_and_keeps_the_question(world: _World) -> None:
    world.shikiru = _Shikiru(error=ShikiruRejectedError("already resolved"))
    op_id = world.install(issued=True)

    world.reconcile()

    assert world.state(op_id) == OpState.VOID
    assert world.signal_state() == SignalState.FORWARDED
    assert world.reshower.shown == [f"install-rejected:{op_id}"]


def test_rejected_recovery_answer_queues_the_question_with_reason_before_voiding(
    world: _World,
) -> None:
    error = ShikiruRejectedError("resolution is not valid for this reason")
    world.shikiru = _Shikiru(error=error)
    op_id = world.install(issued=True)
    _queue_approval(world, op_id)
    forwarder = ProductForwarder(
        80,
        {"api": cast("ShikiruClient", world.shikiru)},
        world.outbox,
        _Clock(),
        FixedDestinations([OWNER]),
        button_label_max_chars=32,
        standing_kinds=STANDING_KINDS,
    )

    asyncio.run(world.reconciler(reshower=forwarder).tick())

    [approval, question] = world.messages.for_signal(world.number)
    assert world.state(op_id) == OpState.VOID
    assert approval.purpose == Purpose.APPROVAL
    assert question.purpose == Purpose.QUESTION
    assert question.delivery == Delivery.PENDING
    assert question.body is not None
    assert str(error) in question.body.text

    asyncio.run(
        forwarder.reshow(
            world.number,
            SIGNAL,
            f"Shikiru rejected the install answer: {error}",
            key=f"install-rejected:{op_id}",
            even_if_closed=True,
        )
    )
    assert len(world.messages.for_signal(world.number)) == 2


def test_rejection_is_durable_when_reshow_lookup_fails_then_escalation_closes(
    world: _World,
) -> None:
    error = ShikiruRejectedError("already resolved by the PO")
    world.shikiru = _Shikiru(
        error=error,
        lookup_error_at=2,
        lookup_error=ShikiruLinkDownError("reshow lookup failed"),
    )
    op_id = world.install(issued=True)
    _queue_approval(world, op_id)
    forwarder = ProductForwarder(
        80,
        {"api": cast("ShikiruClient", world.shikiru)},
        world.outbox,
        _Clock(),
        FixedDestinations([OWNER]),
        button_label_max_chars=32,
        standing_kinds=STANDING_KINDS,
    )

    asyncio.run(world.reconciler(reshower=forwarder).tick())

    pending = world.ops.get(op_id)
    assert pending is not None
    assert pending.state == OpState.ISSUED
    assert pending.install_rejection == str(error)
    assert [message.purpose for message in world.messages.for_signal(world.number)] == [
        Purpose.APPROVAL
    ]

    world.shikiru.close(ResolutionKind.ANSWER)
    asyncio.run(world.reconciler(reshower=forwarder).tick())

    [approval, notice] = world.messages.for_signal(world.number)
    assert world.state(op_id) == OpState.VOID
    assert approval.purpose == Purpose.APPROVAL
    assert notice.purpose == Purpose.NOTICE
    assert notice.delivery == Delivery.PENDING
    assert notice.body is not None
    assert str(error) in notice.body.text
    assert len(world.shikiru.lookups) == 3


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
        (None, ResolutionKind.ANSWER, OpState.VOID),
        (DONE, ResolutionKind.ANSWER, OpState.ISSUED),
        (DONE, f"{ResolutionKind.ANSWER} Installed ffmpeg. Please continue.", OpState.ISSUED),
        (None, ResolutionKind.CANCEL, OpState.VOID),
        (DONE, ResolutionKind.CANCEL, OpState.VOID),
    ],
    ids=[
        "unattributed-answer",
        "answer-without-event",
        "answer-with-text-without-event",
        "cancelled-unacked",
        "cancelled-acked",
    ],
)
def test_an_issued_install_closed_without_matching_event_is_not_confirmed(
    world: _World, ack: Ack | None, resolution: str, expected: str
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, resolution))
    op_id = world.install(issued=True, ack=ack)

    world.reconcile()

    assert world.state(op_id) == expected
    assert world.shikiru.answers == []


def test_a_different_command_event_preserves_the_external_resolution_at_restart(
    world: _World, memory: Database
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, ResolutionKind.ANSWER))
    op_id = world.install(issued=True, ack=DONE)
    events = JournalEvents(memory, CursorStore(memory, world.signals), world.ops)
    events.record_batch(
        "api",
        [
            Closing("api", escalation_id="E-1"),
            Resolved("api", "E-1", ResolutionKind.ANSWER, command_id="99"),
        ],
        1,
        None,
        RESTART,
    )

    world.reconcile()

    assert world.state(op_id) == OpState.VOID
    assert _final_line(memory, world.number) == "Resolved in Shikiru: answer"


def test_an_event_without_its_lost_ack_is_voided_after_restart(
    world: _World, memory: Database
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, ResolutionKind.ANSWER))
    op_id = world.install(issued=True)
    events = JournalEvents(memory, CursorStore(memory, world.signals), world.ops)
    events.record_batch(
        "api",
        [
            Closing("api", escalation_id="E-1"),
            Resolved("api", "E-1", ResolutionKind.ANSWER, command_id=str(DONE.id)),
        ],
        1,
        None,
        RESTART,
    )

    for _ in range(3):
        world.reconcile()

    assert world.state(op_id) == OpState.VOID
    assert world.acknowledgement(op_id) is None
    assert _final_line(memory, world.number) == "Resolved in Shikiru: answer"
    assert world.shikiru.lookups == ["E-1"]


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


def test_an_in_hand_install_waits_for_its_pre_ack_resolution_event(
    world: _World, memory: Database
) -> None:
    world.shikiru = _Shikiru(view=_view(EscalationStatus.RESOLVED, ResolutionKind.ANSWER))
    op_id = world.install(issued=True)
    world.runner.in_hand.add(op_id)
    events = JournalEvents(memory, CursorStore(memory, world.signals), world.ops)
    events.record_batch(
        "api",
        [
            Closing("api", escalation_id="E-1"),
            Resolved("api", "E-1", ResolutionKind.ANSWER, command_id=str(DONE.id)),
        ],
        1,
        None,
        RESTART,
    )

    world.reconcile()

    assert world.shikiru.answers == []
    assert world.state(op_id) == OpState.ISSUED
    assert world.acknowledgement(op_id) is None


def test_queued_answer_is_rechecked_after_engine_recovery_in_the_same_run(world: _World) -> None:
    world.shikiru = _Shikiru(engine_up=False)
    op_id = world.install(issued=True, ack=QUEUED)

    def engine_returns() -> None:
        world.shikiru.engine_up = True
        if world.reshower.shown:
            raise _StopRunError

    reconciler = world.reconciler(_LoopClock(engine_returns), interval_s=120)
    with pytest.raises(_StopRunError):
        asyncio.run(reconciler.run())

    assert world.state(op_id) == OpState.FAILED
    assert world.reshower.shown == [f"reshow:{op_id}"]


def test_an_approval_arriving_after_startup_is_observed_by_the_next_pass(world: _World) -> None:
    new_ops: list[int] = []

    def approval_arrives() -> None:
        if not new_ops:
            new_ops.append(world.install(issued=False))
        elif world.runner.started:
            raise _StopRunError

    reconciler = world.reconciler(_LoopClock(approval_arrives))
    with pytest.raises(_StopRunError):
        asyncio.run(reconciler.run())

    assert world.runner.started == new_ops
    assert world.state(new_ops[0]) == OpState.APPROVED
    assert world.shikiru.lookups == ["E-1"]


def test_a_transient_check_failure_is_retried_during_the_same_run(world: _World) -> None:
    op_id = world.install(issued=True)
    world.check.error = CommandError("temporary failure")

    def check_recovers() -> None:
        world.check.error = None
        if world.shikiru.answers:
            raise _StopRunError

    reconciler = world.reconciler(_LoopClock(check_recovers))
    with pytest.raises(_StopRunError):
        asyncio.run(reconciler.run())

    assert world.check.calls == 2
    assert world.state(op_id) == OpState.ISSUED
    assert len(world.shikiru.answers) == 1


def _final_line(database: Database, number: int) -> str | None:
    row = database.connection.execute(
        "SELECT final_line FROM signals WHERE number = ?", (number,)
    ).fetchone()
    return None if row is None else row["final_line"]


def test_the_final_line_of_an_answered_install_is_set(memory: Database, world: _World) -> None:
    world.install(issued=True)

    world.reconcile()

    assert _final_line(memory, world.number) == FINAL_LINE.format(tool="ffmpeg", manager="brew")
