"""An automatic retry settles like an answer: by its event, or by the reconciler (§10.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.reconciler import RETRY_AGAIN_NOTE, ResolveReconciler
from shikiri_yaku.app.router import Router
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Lifecycle, LifecycleState, Resolved, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalActions,
    JournalEvents,
    JournalOutbox,
    JournalRetries,
    JournalRouting,
    JournalStarts,
    JournalWaits,
    MessageStore,
    NewSignal,
    OpState,
    OpStore,
    SignalState,
    SignalStore,
    StoredOp,
)
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.ports import Ack, EngineStatus
from shikiri_yaku.settings import BUTTON_LABEL_MAX_CHARS, MESSAGE_TITLE_MAX_CHARS
from tests.support.destinations import FixedDestinations
from tests.support.notices import journal_notifier, journal_outbox
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
        self.attempts = 0
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        self.attempts += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release is not None
            await self.release.wait()
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
    def __init__(self, forwarder: ProductForwarder) -> None:
        self._forwarder = forwarder
        self.shown: list[tuple[int, str]] = []

    async def reshow(self, number: int, signal: Signal, note: str, *, key: str) -> None:
        self.shown.append((number, note))
        await self._forwarder.reshow(number, signal, note, key=key)


class Fallback:
    def __init__(self, forwarder: ProductForwarder) -> None:
        self._forwarder = forwarder
        self.forwarded: list[str] = []

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        self.forwarded.append(note)
        await self._forwarder.forward(number, signal, note, suggestion)


class Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class World:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.ops = OpStore(self.database)
        self.number = self.signals.insert_if_absent(NEW_SIGNAL, STARTED).row_id
        self.client = RetryClient()
        self.clients = {"api": cast("ShikiruClient", self.client)}
        self.clock = Clock()
        self.messages = MessageStore(self.database, Reporter())
        outbox = JournalOutbox(self.database, self.messages)
        destinations = FixedDestinations(
            [Destination("telegram", "owner-1"), Destination("telegram", "owner-2")]
        )
        forwarder = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            self.clients,
            outbox,
            self.clock,
            destinations,
            button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
        )
        self.reshower = Reshower(forwarder)
        self.fallback = Fallback(forwarder)
        retries = JournalRetries(self.database, self.ops)
        guard = LoopGuard(JournalActions(self.ops), self.clock, 1, 1)
        waits = JournalWaits(self.database, self.ops)
        notifier = journal_notifier(journal_outbox(self.database), self.clock)
        self.infrastructure = infrastructure_extension(
            self.clients,
            guard,
            retries,
            self.clock,
            waits=waits,
            starts=JournalStarts(self.database, self.ops, self.signals),
            notifier=notifier,
            quota_wait_max=timedelta(hours=24),
            quota_wait_margin=timedelta(minutes=1),
            restart_confirm=timedelta(seconds=60),
            restart_poll=timedelta(seconds=5),
        )
        extensions = [self.infrastructure, product_extension()]
        self.classifier = RuleClassifier(extensions)
        self.routing = JournalRouting(self.database, self.signals)
        self.router = Router(
            self.classifier,
            {extension.category: extension.handler for extension in extensions},
            self.fallback,
            self.routing,
            self.clock,
            handler_timeout_s=60,
            forward_retry_s=60,
        )

    async def begin_retry(self) -> None:
        classification = await self.classifier.classify(SIGNAL)
        assert self.routing.begin(
            self.number, classification, self.infrastructure.category, self.clock.now()
        )
        await self.infrastructure.handler.handle(SIGNAL)

    def crash_before_ack(self) -> None:
        async def run() -> None:
            self.client.entered = asyncio.Event()
            self.client.release = asyncio.Event()
            task = asyncio.create_task(self.begin_retry())
            await self.client.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def crash_after_ack(self) -> None:
        asyncio.run(self.begin_retry())

    def recover_and_dispatch(self) -> None:
        async def run() -> None:
            self.routing.recover(self.clock.now())
            dispatcher = Dispatcher(self.routing, self.router, self.clock, interval_s=60)
            dispatcher.tick()
            await self.router.join()

        asyncio.run(run())

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

    def question_chats(self) -> list[str]:
        return [
            message.chat
            for message in self.messages.for_signal(self.number)
            if message.purpose == Purpose.QUESTION
        ]

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


def test_recovered_queued_retry_waits_for_confirmation_without_forwarding(world: World) -> None:
    world.crash_after_ack()
    assert world.restart() == set()
    world.recover_and_dispatch()
    assert (
        world.client.attempts,
        world.retry_op().state,
        world.signal_state(),
        world.question_chats(),
    ) == (
        1,
        OpState.ISSUED,
        SignalState.RESOLVED,
        [],
    )

    world.confirm_by_event("retry")
    world.tick()
    assert (world.retry_op().state, world.reshower.shown, world.question_chats()) == (
        OpState.CONFIRMED,
        [],
        [],
    )


@pytest.mark.parametrize(
    ("crash_point", "order"),
    [
        ("before_ack", "dispatch_first"),
        ("before_ack", "reconcile_first"),
        ("after_ack", "dispatch_first"),
        ("after_ack", "reconcile_first"),
    ],
)
def test_reconciliation_and_dispatch_queue_one_recovered_question_per_destination(
    world: World, crash_point: str, order: str
) -> None:
    if crash_point == "after_ack":
        world.crash_after_ack()
        world.clock.at = STARTED + timedelta(minutes=LOST_MIN)
    else:
        world.crash_before_ack()
    assert world.signal_state() == SignalState.HANDLING
    uncertain = world.restart()

    if order == "reconcile_first":
        world.tick(uncertain)
        world.recover_and_dispatch()
    else:
        world.recover_and_dispatch()
        world.tick(uncertain)

    assert (world.client.attempts, world.retry_op().state, world.signal_state()) == (
        1,
        OpState.FAILED,
        SignalState.FORWARDED,
    )
    assert world.question_chats() == ["owner-1", "owner-2"]


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
    assert world.question_chats() == ["owner-1", "owner-2"]
    assert "escalation already resolved" in world.fallback.forwarded[0]
    world.clock.at = STARTED + timedelta(hours=1)
    world.tick()
    assert world.reshower.shown == []
