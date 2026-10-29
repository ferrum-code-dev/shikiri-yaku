from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.router import Router
from shikiri_yaku.domain.outcome import Forward, Resolved
from shikiri_yaku.domain.rule_classifier import RegistrationError, RuleClassifier
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.retry import RetryAction
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalRouting,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import Ack, AckStatus

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from shikiri_yaku.domain.extension import Handler
    from shikiri_yaku.ports import ShikiruClient

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TASK = "T-1"
RETRY_REASONS = (
    "agent_error",
    "agent_stuck",
    "agent_protocol",
    "integration_conflict",
    "integration_blocked",
)


class FakeClock:
    def now(self) -> datetime:
        return START

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class FakeClient:
    """Shikiru as the retry sees it: it records each resolution and acknowledges it."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, str]] = []

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        del text
        self.resolved.append((escalation_id, kind))
        return Ack(id=1, status=AckStatus.DONE, result=None, message=None)


class FakeFallback:
    def __init__(self) -> None:
        self.forwarded: list[str] = []

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        del number, signal, suggestion
        self.forwarded.append(note)


def escalation(reason: str, source: str = "E1") -> Signal:
    return Signal("api", Kind.ESCALATION, source, reason, "blocked", TASK, source)


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def clients(client: FakeClient) -> Mapping[str, ShikiruClient]:
    return {"api": cast("ShikiruClient", client)}


@pytest.fixture
def guard(memory: Database) -> LoopGuard:
    return LoopGuard(JournalActions(OpStore(memory)), FakeClock(), 1, 1)


@pytest.fixture
def handler(clients: Mapping[str, ShikiruClient], guard: LoopGuard) -> Handler:
    return infrastructure_extension(clients, guard).handler


@pytest.mark.parametrize("reason", RETRY_REASONS)
def test_each_retry_reason_resolves_the_escalation_once(
    handler: Handler, client: FakeClient, reason: str
) -> None:
    outcome = asyncio.run(handler.handle(escalation(reason)))

    assert isinstance(outcome, Resolved)
    assert client.resolved == [("E1", "retry")]


def test_a_second_retry_of_the_same_task_and_reason_forwards_naming_the_first(
    handler: Handler, client: FakeClient
) -> None:
    first = asyncio.run(handler.handle(escalation("agent_error", "E1")))
    second = asyncio.run(handler.handle(escalation("agent_error", "E2")))

    assert isinstance(first, Resolved)
    assert isinstance(second, Forward)
    assert START.isoformat() in second.note
    assert client.resolved == [("E1", "retry")]


@pytest.mark.parametrize(
    ("kind", "reason", "named"),
    [
        (Kind.ESCALATION, "ladder_exhausted", "ladder_exhausted"),
        (Kind.ESCALATION, "quota_exhausted", "quota_exhausted"),
        (Kind.ESCALATION, "engine_error", "engine_error"),
        (Kind.ENGINE_DOWN, "", "engine_down"),
        (Kind.ENGINE_FAULT, "", "engine_fault"),
    ],
)
def test_a_signal_no_action_covers_forwards_naming_it(
    handler: Handler, client: FakeClient, kind: Kind, reason: str, named: str
) -> None:
    outcome = asyncio.run(handler.handle(Signal("api", kind, "S1", reason, "text")))

    assert isinstance(outcome, Forward)
    assert named in outcome.note
    assert client.resolved == []


def test_two_actions_claiming_one_reason_fail_at_registration(
    clients: Mapping[str, ShikiruClient], guard: LoopGuard
) -> None:
    with pytest.raises(RegistrationError, match="agent_error"):
        InfrastructureHandler([RetryAction(clients, guard), RetryAction(clients, guard)])


def test_an_agent_error_resolves_and_the_po_is_not_told(
    memory: Database, clients: Mapping[str, ShikiruClient], guard: LoopGuard, client: FakeClient
) -> None:
    extensions = [infrastructure_extension(clients, guard), product_extension()]
    fallback = FakeFallback()
    router = Router(
        RuleClassifier(extensions),
        {extension.category: extension.handler for extension in extensions},
        fallback,
        JournalRouting(memory, SignalStore(memory)),
        FakeClock(),
        handler_timeout_s=60,
        forward_retry_s=60,
    )
    signal = escalation("agent_error")
    new = NewSignal(signal.id, "api", "escalation", "agent_error", "blocked", TASK, "E1")
    number = SignalStore(memory).insert_if_absent(new, START).row_id

    async def run() -> None:
        router.submit(number, signal)
        await router.join()

    asyncio.run(run())

    stored = SignalStore(memory).get(number)
    assert stored is not None
    assert stored.state is SignalState.RESOLVED
    assert client.resolved == [("E1", "retry")]
    assert fallback.forwarded == []
    assert int(memory.connection.execute("SELECT count(*) FROM messages").fetchone()[0]) == 0
