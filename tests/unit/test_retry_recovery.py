"""An automatic retry settles like an answer: by its event, or by the reconciler (§10.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.reconciler import RETRY_AGAIN_NOTE, ResolveReconciler
from shikiri_yaku.app.router import Router
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Kind, Lifecycle, LifecycleState, Resolved, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalActions,
    JournalEvents,
    JournalRetries,
    JournalRouting,
    NewSignal,
    OpState,
    OpStore,
    SignalState,
    SignalStore,
    StoredOp,
)
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.ports import Ack, EngineStatus
from tests.unit.test_replies import Client

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import ShikiruClient

STARTED: Final = datetime(2026, 10, 28, tzinfo=UTC)
LOST_MIN: Final = 2
SIGNAL: Final = Signal("api", Kind.ESCALATION, "E-1", "agent_error", "blocked", "T-1", "E-1")
NEW_SIGNAL: Final = NewSignal(
    "api:escalation:E-1", "api", "escalation", "agent_error", "blocked", "T-1", "E-1"
)


class RetryClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.engine_up = True

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        return await super().resolve_escalation(escalation_id, kind, text)

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=self.engine_up, quotas={})

    def resolve_in_shikiru(self, resolution: str) -> None:
        record = self.view.escalation.model_copy(
            update={"status": "resolved", "resolution": resolution}
        )
        self.view = self.view.model_copy(update={"escalation": record})


class Lifecycles:
    def state(self, project: str) -> LifecycleState:  # noqa: ARG002 -- every project started alike
        return LifecycleState(1, Lifecycle(1, "engine_started", has_reason=False, at=STARTED), None)

    def respect_stop(self, project: str, event_id: int) -> None:
        raise NotImplementedError


class Clock:
    def __init__(self) -> None:
        self.at = STARTED

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Reshower:
    def __init__(self) -> None:
        self.shown: list[tuple[int, str]] = []

    async def reshow(self, number: int, signal: Signal, note: str, *, key: str) -> None:
        del signal, key
        self.shown.append((number, note))


class Fallback:
    def __init__(self) -> None:
        self.forwarded: list[str] = []

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        del number, signal, suggestion
        self.forwarded.append(note)


class World:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.ops = OpStore(self.database)
        self.number = self.signals.insert_if_absent(NEW_SIGNAL, STARTED).row_id
        self.client = RetryClient()
        self.clients = {"api": cast("ShikiruClient", self.client)}
        self.clock = Clock()
        self.reshower = Reshower()
        self.fallback = Fallback()
        retries = JournalRetries(self.database, self.ops)
        guard = LoopGuard(JournalActions(self.ops), self.clock, 1, 1)
        extensions = [
            infrastructure_extension(self.clients, guard, retries, self.clock),
            product_extension(),
        ]
        self.router = Router(
            RuleClassifier(extensions),
            {extension.category: extension.handler for extension in extensions},
            self.fallback,
            JournalRouting(self.database, self.signals),
            self.clock,
            handler_timeout_s=60,
            forward_retry_s=60,
        )

    def route(self) -> None:
        async def run() -> None:
            self.router.submit(self.number, SIGNAL)
            await self.router.join()

        asyncio.run(run())

    def reconciler(self) -> ResolveReconciler:
        return ResolveReconciler(
            JournalReconcile(self.database, self.signals, self.ops),
            Lifecycles(),
            self.reshower,
            self.clients,
            self.clock,
            lost_command_min=LOST_MIN,
            interval_s=60,
        )

    def tick(self, uncertain: set[int] | None = None) -> None:
        asyncio.run(self.reconciler().tick(set() if uncertain is None else uncertain))

    def restart(self) -> set[int]:
        log = JournalReconcile(self.database, self.signals, self.ops)
        return {op.op_id for op in log.unsettled() if op.ack is None}

    def retry_op(self) -> StoredOp:
        # keep: the guard journals its own retry row first, so the resolve is found by kind
        row = self.database.connection.execute(
            "SELECT id FROM ops WHERE kind = 'resolve'"
        ).fetchone()
        op = self.ops.get(row["id"])
        assert op is not None
        return op

    def signal_state(self) -> SignalState:
        signal = self.signals.get(self.number)
        assert signal is not None
        return signal.state

    def confirm_by_event(self, resolution: str) -> None:
        events = JournalEvents(self.database, CursorStore(self.database, self.signals), self.ops)
        events.record_batch("api", [Resolved("api", "E-1", resolution, "12")], 5, None, STARTED)


@pytest.fixture
def world() -> Generator[World]:
    world = World()
    yield world
    world.database.close()


def test_queued_retry_keeps_its_command_and_is_lost_when_still_open_after_the_wait(
    world: World,
) -> None:
    world.route()
    op = world.retry_op()
    assert (op.state, op.command_id, world.signal_state()) == (
        OpState.ISSUED,
        "12",
        SignalState.RESOLVED,
    )
    world.clock.at = STARTED + timedelta(minutes=LOST_MIN) - timedelta(seconds=1)
    world.tick()
    assert (world.retry_op().state, world.reshower.shown) == (OpState.ISSUED, [])
    world.clock.at = STARTED + timedelta(minutes=LOST_MIN)
    world.tick()
    assert (world.retry_op().state, world.signal_state()) == (
        OpState.FAILED,
        SignalState.FORWARDED,
    )
    assert world.reshower.shown == [(world.number, RETRY_AGAIN_NOTE)]


def test_the_event_naming_the_retry_confirms_it_and_nothing_is_shown_later(world: World) -> None:
    world.route()
    world.confirm_by_event("retry")
    world.clock.at = STARTED + timedelta(hours=1)
    world.tick()
    assert (world.retry_op().state, world.reshower.shown) == (OpState.CONFIRMED, [])


def test_a_retry_cut_off_before_its_acknowledgement_is_shown_again_while_open(
    world: World,
) -> None:
    world.client.fail = True
    world.route()
    uncertain = world.restart()
    assert len(uncertain) == 1
    world.tick(uncertain)
    assert (world.retry_op().state, world.reshower.shown) == (
        OpState.FAILED,
        [(world.number, RETRY_AGAIN_NOTE)],
    )


@pytest.mark.parametrize(
    ("resolution", "state"), [("retry", OpState.CONFIRMED), ("answer", OpState.FAILED)]
)
def test_a_retry_cut_off_after_the_escalation_was_resolved_is_decided_by_how_it_was_resolved(
    world: World, resolution: str, state: OpState
) -> None:
    world.client.fail = True
    world.route()
    world.client.resolve_in_shikiru(resolution)
    world.tick(world.restart())
    assert (world.retry_op().state, world.reshower.shown) == (state, [])


def test_a_rejected_retry_fails_its_op_and_the_po_is_told_why(world: World) -> None:
    world.client.reject = True
    world.route()
    assert (world.retry_op().state, world.signal_state()) == (
        OpState.FAILED,
        SignalState.FORWARDED,
    )
    assert len(world.fallback.forwarded) == 1
    assert "escalation already resolved" in world.fallback.forwarded[0]
    world.clock.at = STARTED + timedelta(hours=1)
    world.tick()
    assert world.reshower.shown == []
