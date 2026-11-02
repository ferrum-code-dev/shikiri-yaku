from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.router import Router
from shikiri_yaku.domain.outcome import Forward, Resolved
from shikiri_yaku.domain.rule_classifier import RegistrationError, RuleClassifier
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.rules import AGENT_ERROR
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalRetries,
    JournalRouting,
    JournalStarts,
    JournalWaits,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import Ack, AckStatus
from tests.support.install import UnusedInstallAction
from tests.support.notices import journal_notifier, journal_outbox

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from shikiri_yaku.domain.extension import Extension, Handler
    from shikiri_yaku.domain.outcome import Outcome
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

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> bool:
        del number, signal, suggestion
        self.forwarded.append(note)
        return False


class ClaimsAgentError:
    """Claims one reason only, so a duplicate is reported by name, whatever the set's order."""

    reasons = frozenset({AGENT_ERROR})

    async def run(self, signal: Signal) -> Outcome:
        raise NotImplementedError


def journal_escalation(memory: Database, reason: str, source: str = "E1") -> tuple[int, Signal]:
    """Journal the escalation as the Router would, so a retry can find its number."""
    signal = Signal("api", Kind.ESCALATION, source, reason, "blocked", TASK, source)
    new = NewSignal(signal.id, "api", "escalation", reason, "blocked", TASK, source)
    return SignalStore(memory).insert_if_absent(new, START).row_id, signal


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
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def retries(memory: Database) -> JournalRetries:
    return JournalRetries(memory, OpStore(memory))


@pytest.fixture
def guard(memory: Database, clock: FakeClock) -> LoopGuard:
    return LoopGuard(JournalActions(OpStore(memory)), clock, 1, 1)


@pytest.fixture
def infrastructure(
    memory: Database,
    clients: Mapping[str, ShikiruClient],
    guard: LoopGuard,
    retries: JournalRetries,
    clock: FakeClock,
) -> Extension:
    waits = JournalWaits(memory, OpStore(memory))
    notifier = journal_notifier(journal_outbox(memory), clock)
    return infrastructure_extension(
        clients,
        guard,
        retries,
        clock,
        waits=waits,
        starts=JournalStarts(memory, OpStore(memory), SignalStore(memory)),
        notifier=notifier,
        install=UnusedInstallAction(),
        quota_wait_max=timedelta(hours=24),
        quota_wait_margin=timedelta(minutes=1),
        restart_confirm=timedelta(seconds=60),
        restart_poll=timedelta(seconds=5),
    )


@pytest.fixture
def handler(infrastructure: Extension) -> Handler:
    return infrastructure.handler


@pytest.mark.parametrize("reason", RETRY_REASONS)
def test_each_retry_reason_resolves_the_escalation_once(
    memory: Database, handler: Handler, client: FakeClient, reason: str
) -> None:
    _, signal = journal_escalation(memory, reason)

    outcome = asyncio.run(handler.handle(signal))

    assert isinstance(outcome, Resolved)
    assert client.resolved == [("E1", "retry")]


def test_a_second_retry_of_the_same_task_and_reason_forwards_naming_the_first(
    memory: Database, handler: Handler, client: FakeClient
) -> None:
    _, first_signal = journal_escalation(memory, "agent_error", "E1")
    _, second_signal = journal_escalation(memory, "agent_error", "E2")

    first = asyncio.run(handler.handle(first_signal))
    second = asyncio.run(handler.handle(second_signal))

    assert isinstance(first, Resolved)
    assert isinstance(second, Forward)
    assert "was at 09:00 UTC." in second.note
    assert client.resolved == [("E1", "retry")]


@pytest.mark.parametrize(
    ("kind", "reason", "named"),
    [
        (Kind.HEALTH, "", "health"),
    ],
)
def test_a_signal_no_action_covers_forwards_naming_it(
    handler: Handler, client: FakeClient, kind: Kind, reason: str, named: str
) -> None:
    outcome = asyncio.run(handler.handle(Signal("api", kind, "S1", reason, "text")))

    assert isinstance(outcome, Forward)
    assert named in outcome.note
    assert client.resolved == []


def test_two_actions_claiming_one_reason_fail_at_registration() -> None:
    with pytest.raises(RegistrationError, match=f"reason '{AGENT_ERROR}'"):
        InfrastructureHandler([ClaimsAgentError(), ClaimsAgentError()])


def test_an_agent_error_resolves_and_the_po_is_not_told(
    memory: Database, infrastructure: Extension, client: FakeClient
) -> None:
    clock = FakeClock()
    extensions = [infrastructure, product_extension()]
    fallback = FakeFallback()
    router = Router(
        RuleClassifier(extensions),
        {extension.category: extension.handler for extension in extensions},
        fallback,
        JournalRouting(memory, SignalStore(memory)),
        clock,
        handler_timeout_s=60,
        forward_retry_s=60,
    )
    number, signal = journal_escalation(memory, "agent_error")

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
