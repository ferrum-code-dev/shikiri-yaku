from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.reconciler import ResolveReconciler
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Lifecycle, LifecycleState
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import OpState
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.journal.signals import NewSignal, SignalState
from shikiri_yaku.ports import (
    Ack,
    EngineStatus,
    EscalationView,
    ResolvePayload,
    ShikiruClient,
    ShikiruLinkDownError,
)
from shikiri_yaku.settings import MESSAGE_TITLE_MAX_CHARS
from tests.support.destinations import FixedDestinations
from tests.unit.test_replies import Client, Harness

if TYPE_CHECKING:
    from collections.abc import Generator

STARTED: Final = datetime(2026, 10, 28, tzinfo=UTC)
LOST_MIN: Final = 2


class _Client(Client):
    def __init__(self) -> None:
        super().__init__()
        self.engine_up = True
        self.ack_status = "queued"
        self.down = False

    async def get_escalation(self, escalation_id: str) -> EscalationView:
        if self.down:
            message = "link is down"
            raise ShikiruLinkDownError(message)
        return await super().get_escalation(escalation_id)

    async def resolve_escalation(self, escalation_id: str, kind: str, text: str | None) -> Ack:
        await super().resolve_escalation(escalation_id, kind, text)
        return Ack(id=12, status=self.ack_status, result=None, message=None)

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=self.engine_up, quotas={})


class _Lifecycles:
    def state(self, project: str) -> LifecycleState:  # noqa: ARG002 -- every project started alike
        return LifecycleState(1, Lifecycle(1, "engine_started", has_reason=False, at=STARTED), None)

    def respect_stop(self, project: str, event_id: int) -> None:
        raise NotImplementedError


class _Clock:
    def __init__(self) -> None:
        self.at = STARTED

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _World:
    def __init__(self) -> None:
        self.h = Harness()
        self.client = _Client()
        self.h.client = self.client
        self.h.clients["api"] = cast("ShikiruClient", self.client)
        self.clock = _Clock()

    def reconciler(self) -> ResolveReconciler:
        h = self.h
        forwarder = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            h.clients,
            h.outbox,
            self.clock,
            FixedDestinations([Destination("telegram", "1001")]),
        )
        return ResolveReconciler(
            JournalReconcile(h.database, h.signals, h.ops),
            _Lifecycles(),
            forwarder,
            h.clients,
            self.clock,
            lost_command_min=LOST_MIN,
            interval_s=60,
        )

    def restart(self) -> set[int]:
        log = JournalReconcile(self.h.database, self.h.signals, self.h.ops)
        return {op.op_id for op in log.unsettled() if op.ack is None}

    def tick(self, uncertain: set[int] | None = None) -> None:
        asyncio.run(self.reconciler().tick(set() if uncertain is None else uncertain))

    def resolve_in_shikiru(self, resolution: str) -> None:
        record = self.client.view.escalation.model_copy(
            update={"status": "resolved", "resolution": resolution}
        )
        self.client.view = self.client.view.model_copy(update={"escalation": record})

    def signal_state(self) -> SignalState:
        signal = self.h.signals.get(self.h.number)
        assert signal is not None
        return signal.state

    def reshown(self) -> int:
        return len(self.h.outbox.pending())


@pytest.fixture
def world() -> Generator[_World]:
    world = _World()
    world.h.signals.transition(world.h.number, SignalState.NEW, SignalState.FORWARDED, STARTED)
    yield world
    world.h.database.close()


def _only_op(world: _World) -> OpState:
    op = world.h.ops.get(1)
    assert op is not None
    return op.state


def test_queued_resolve_still_open_after_the_wait_is_lost_and_the_question_returns(
    world: _World,
) -> None:
    world.h.handle(data="a:1:1")
    world.clock.at = STARTED + timedelta(minutes=LOST_MIN) - timedelta(seconds=1)
    world.tick()
    assert (_only_op(world), world.reshown()) == (OpState.ISSUED, 0)
    world.clock.at = STARTED + timedelta(minutes=LOST_MIN)
    world.tick()
    world.tick()
    assert (_only_op(world), world.signal_state(), world.reshown()) == (
        OpState.FAILED,
        SignalState.FORWARDED,
        1,
    )


def test_queued_resolve_waits_while_the_engine_is_down_or_the_escalation_is_resolved(
    world: _World,
) -> None:
    world.h.handle(data="a:1:1")
    world.clock.at = STARTED + timedelta(hours=1)
    world.client.engine_up = False
    world.tick()
    world.client.engine_up = True
    world.resolve_in_shikiru("answer")
    world.tick()
    assert (_only_op(world), world.reshown()) == (OpState.ISSUED, 0)


def test_crash_mid_send_with_the_escalation_open_shows_the_question_again(world: _World) -> None:
    world.client.fail = True
    with pytest.raises(ShikiruLinkDownError):
        world.h.handle(data="a:1:1")
    uncertain = world.restart()
    assert len(uncertain) == 1
    world.tick(uncertain)
    assert (_only_op(world), world.signal_state(), world.reshown()) == (
        OpState.FAILED,
        SignalState.FORWARDED,
        1,
    )


@pytest.mark.parametrize(
    ("resolution", "state", "shown"),
    [("answer", OpState.CONFIRMED, 0), ("cancel", OpState.FAILED, 0)],
)
def test_crash_mid_send_with_the_escalation_resolved_is_decided_by_how_it_was_resolved(
    world: _World, resolution: str, state: OpState, shown: int
) -> None:
    world.client.fail = True
    with pytest.raises(ShikiruLinkDownError):
        world.h.handle(data="a:1:1")
    world.resolve_in_shikiru(resolution)
    world.tick(world.restart())
    assert (_only_op(world), world.reshown()) == (state, shown)


def test_an_op_issued_after_the_start_is_not_uncertain(world: _World) -> None:
    uncertain = world.restart()
    world.client.fail = True
    with pytest.raises(ShikiruLinkDownError):
        world.h.handle(data="a:1:1")
    world.tick(uncertain)
    assert (_only_op(world), world.reshown()) == (OpState.ISSUED, 0)


def test_acknowledged_done_resolve_is_left_to_its_event(world: _World) -> None:
    world.client.ack_status = "done"
    world.h.handle(data="a:1:1")
    world.clock.at = STARTED + timedelta(hours=1)
    world.tick()
    assert (_only_op(world), world.reshown()) == (OpState.ISSUED, 0)


def test_one_project_being_unreachable_does_not_block_the_others(world: _World) -> None:
    h = world.h
    world.client.fail = True
    with pytest.raises(ShikiruLinkDownError):
        h.handle(data="a:1:1")
    web = NewSignal("web:escalation:E-1", "web", "escalation", "agent_error", "q", "T-2", "E-1")
    number = h.signals.insert_if_absent(web, STARTED).row_id
    h.signals.transition(number, SignalState.NEW, SignalState.FORWARDED, STARTED)
    payload = ResolvePayload(
        number=number,
        project="web",
        task_id="T-2",
        reason="agent_error",
        escalation_id="E-1",
        kind="retry",
        text=None,
        label="retry",
    )
    log = h.log()
    queued = log.intend_resolve(900, payload, STARTED)
    assert queued is not None
    assert log.claim_resolve(queued, STARTED) is not None
    log.acknowledge_resolve(queued, Ack(id=3, status="queued", result=None, message=None), STARTED)
    h.clients["web"] = cast("ShikiruClient", _Client())
    uncertain = world.restart()
    world.client.down = True
    world.clock.at = STARTED + timedelta(minutes=LOST_MIN)
    world.tick(uncertain)
    states = {op.id: op.state for op in h.ops.non_final()}
    assert states == {1: OpState.ISSUED}
    world.client.down = False
    world.tick(uncertain)
    assert h.ops.non_final() == []
