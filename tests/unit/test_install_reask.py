from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.install_reask import InstallReasker
from shikiri_yaku.domain.callback import install_data
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.install import InstallApprovalPresenter
from shikiri_yaku.extensions.infrastructure.install_replies import InstallApprovalReplies
from shikiri_yaku.journal import (
    Database,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.ports import Button, EscalationStatus, Message, ReplyOrigin
from tests.support.clock import NapClock
from tests.support.destinations import FixedDestinations
from tests.support.notices import FailingReporter

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import Forwarder, InstallOp, ShikiruClient

NOW: Final = datetime(2026, 10, 31, tzinfo=UTC)
OWNER: Final = Destination("telegram", "1001")
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


class _World:
    """One forwarded escalation with an issued install op, and the re-asker over the journal."""

    def __init__(self, database: Database) -> None:
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
        self.ops = OpStore(database)
        self.messages = MessageStore(database, FailingReporter())
        self.outbox = JournalOutbox(database, self.messages)
        self.clock = NapClock()
        self.reasker = InstallReasker(
            self.ops,
            InstallApprovalPresenter(),
            self.outbox,
            FixedDestinations([OWNER]),
            self.clock,
        )

    def issued(self) -> InstallOp:
        payload = PAYLOAD.model_copy(update={"number": self.number})
        operation = self.ops.intend_install(f"install:{SIGNAL.id}", SIGNAL, payload, NOW)
        assert self.ops.transition(operation.id, OpState.INTENDED, OpState.APPROVED, NOW)
        assert self.ops.transition(operation.id, OpState.APPROVED, OpState.ISSUED, NOW)
        return operation

    def reasked(self, old: InstallOp) -> InstallOp:
        identity = f"install:{SIGNAL.id}:after:{old.id}"
        operation = self.ops.get_install(identity)
        assert operation is not None
        return operation


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def test_ask_again_queues_the_stored_command_and_then_voids_the_old_op(memory: Database) -> None:
    world = _World(memory)
    old = world.issued()

    world.reasker.ask_again(old, SIGNAL)

    new = world.reasked(old)
    assert new.payload == old.payload
    [stored] = world.messages.for_signal(world.number)
    assert stored.purpose is Purpose.APPROVAL
    assert stored.body is not None
    assert stored.body.buttons[0].data == install_data(world.number, new.id)
    old_now = world.ops.install_op(old.id)
    assert old_now is not None
    assert old_now.state == OpState.VOID


def test_repeat_after_a_crash_queues_one_approval(memory: Database) -> None:
    world = _World(memory)
    old = world.issued()
    world.reasker.ask_again(old, SIGNAL)
    # keep: the crash came after the approval was queued and before the old op was voided
    assert world.ops.transition(old.id, OpState.VOID, OpState.ISSUED, NOW)

    world.reasker.ask_again(old, SIGNAL)

    assert len(world.messages.for_signal(world.number)) == 1
    assert memory.connection.execute("SELECT count(*) FROM ops").fetchone()[0] == 2


def test_repeat_does_not_queue_an_approval_the_po_has_already_answered(memory: Database) -> None:
    world = _World(memory)
    old = world.issued()
    world.reasker.ask_again(old, SIGNAL)
    new = world.reasked(old)
    assert world.ops.approve_install(new.id, world.number, NOW)
    assert world.ops.transition(old.id, OpState.VOID, OpState.ISSUED, NOW)

    world.reasker.ask_again(old, SIGNAL)

    assert len(world.messages.for_signal(world.number)) == 1


class _OpenShikiru:
    """Shikiru reports every escalation open, which the tap checks before it acts."""

    async def get_escalation(self, escalation_id: str) -> SimpleNamespace:
        del escalation_id
        return SimpleNamespace(escalation=SimpleNamespace(status=EscalationStatus.OPEN))


class _Starts:
    def __init__(self) -> None:
        self.started: list[int] = []

    def start(self, op_id: int) -> None:
        self.started.append(op_id)


def _approval_origin(world: _World, op_id: int) -> ReplyOrigin:
    buttons = (Button("Install", install_data(world.number, op_id)),)
    return ReplyOrigin(
        world.number,
        SIGNAL,
        Purpose.APPROVAL,
        Message("approval", buttons),
        f"install:{op_id}",
        OWNER,
    )


def test_tap_on_the_replaced_approval_is_void_and_tap_on_the_new_one_runs(memory: Database) -> None:
    world = _World(memory)
    old = world.issued()
    world.reasker.ask_again(old, SIGNAL)
    new = world.reasked(old)
    starts = _Starts()
    replies = InstallApprovalReplies(
        world.ops,
        {"api": cast("ShikiruClient", _OpenShikiru())},
        cast("Forwarder", object()),
        world.outbox,
        world.clock,
        approval_expiry_h=24,
        runner=starts,
    )

    async def tap_both() -> None:
        for op in (old, new):
            item = Inbound(
                1, InboundKind.TAP, 1, 1001, "approval", data=install_data(world.number, op.id)
            )
            await replies.handle(item, _approval_origin(world, op.id), verb="i", op_id=op.id)

    asyncio.run(tap_both())

    notices = [s for s in world.messages.for_signal(world.number) if s.purpose is Purpose.NOTICE]
    assert len(notices) == 1
    assert starts.started == [new.id]
    approved = world.ops.install_op(new.id)
    assert approved is not None
    assert approved.state == OpState.APPROVED
