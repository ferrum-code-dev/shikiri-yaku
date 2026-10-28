from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.router import Router
from shikiri_yaku.domain.classification import Category, Classification, Source
from shikiri_yaku.domain.outcome import Forward, Resolved
from shikiri_yaku.journal import Database, JournalRouting, NewSignal, SignalState, SignalStore
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
INFRA = Category("infrastructure")
STEP_S = 0.001


async def _nap() -> None:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(STEP_S):
            await asyncio.Event().wait()


class _Clock:
    """Real waiting, shortened, so the service loops run for a few milliseconds."""

    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        del seconds
        await _nap()


class _Classifier:
    async def classify(self, signal: Signal) -> Classification:
        del signal
        return Classification(INFRA, Source.RULE)


class _Handler:
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        self.handled: list[str] = []
        self.running = 0
        self.most_running = 0
        self.hold = False
        self.gate = asyncio.Event()

    async def handle(self, signal: Signal) -> Outcome:
        self.handled.append(signal.source_id)
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        if self.hold:
            await self.gate.wait()
        self.running -= 1
        return self.outcome


class _Fallback:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.forwarded: list[str] = []

    async def forward(self, signal: Signal, note: str, suggestion: str) -> None:
        del note, suggestion
        if self.failures:
            self.failures -= 1
            message = "chat is down"
            raise ChannelError(message)
        self.forwarded.append(signal.source_id)


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _insert(signals: SignalStore, name: str) -> int:
    signal = NewSignal(f"api:escalation:{name}", "api", "escalation", "agent_error", "x")
    return signals.insert_if_absent(signal, NOW).row_id


def _service(
    database: Database, handler: _Handler, fallback: _Fallback
) -> tuple[Dispatcher, SignalStore]:
    signals = SignalStore(database)
    routing = JournalRouting(database, signals)
    clock = _Clock()
    router = Router(
        _Classifier(),
        {INFRA: handler},
        fallback,
        routing,
        clock,
        handler_timeout_s=1,
        forward_retry_s=STEP_S,
    )
    return Dispatcher(routing, router, clock, interval_s=STEP_S), signals


async def _run_until(dispatcher: Dispatcher, done: object) -> None:
    service = asyncio.create_task(dispatcher.run())
    try:
        async with asyncio.timeout(2):
            while not done():  # type: ignore[operator]
                await _nap()
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert not service.done() or service.cancelled()


def test_a_new_signal_reaches_its_handler_and_two_run_together(memory: Database) -> None:
    handler = _Handler(Resolved("done"))
    handler.hold = True
    dispatcher, signals = _service(memory, handler, _Fallback())
    first, second = _insert(signals, "E1"), _insert(signals, "E2")

    def both_done() -> bool:
        if handler.running == 2:
            handler.gate.set()
        return len(handler.handled) == 2 and handler.running == 0

    asyncio.run(_run_until(dispatcher, both_done))

    assert handler.most_running == 2
    assert {signals.get(n).state for n in (first, second)} == {SignalState.RESOLVED}  # type: ignore[union-attr]


def test_a_signal_is_submitted_once_while_it_is_handled(memory: Database) -> None:
    handler = _Handler(Resolved("done"))
    handler.hold = True
    dispatcher, signals = _service(memory, handler, _Fallback())
    number = _insert(signals, "E1")

    def finished() -> bool:
        handler.gate.set()
        return bool(handler.handled) and not handler.running

    asyncio.run(_run_until(dispatcher, finished))

    assert handler.handled == ["E1"]
    assert signals.get(number).state is SignalState.RESOLVED  # type: ignore[union-attr]


def test_after_a_crash_only_the_signals_still_in_handling_run_again(memory: Database) -> None:
    handler = _Handler(Resolved("again"))
    dispatcher, signals = _service(memory, handler, _Fallback())
    ends = {
        "E1": SignalState.HANDLING,
        "E2": SignalState.RESOLVED,
        "E3": SignalState.FORWARDED,
        "E4": SignalState.PARKED,
    }
    for name, end in ends.items():
        number = _insert(signals, name)
        signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
        if end is not SignalState.HANDLING:
            signals.transition(number, SignalState.HANDLING, end, NOW + timedelta(hours=1))

    asyncio.run(_run_until(dispatcher, lambda: handler.handled and not handler.running))

    assert handler.handled == ["E1"]


def test_a_forward_that_fails_once_is_retried_and_ends_forwarded(
    memory: Database, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _Handler(Forward("cannot fix"))
    fallback = _Fallback(failures=1)
    dispatcher, signals = _service(memory, handler, fallback)
    number = _insert(signals, "E1")

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run_until(dispatcher, lambda: fallback.forwarded))

    assert signals.get(number).state is SignalState.FORWARDED  # type: ignore[union-attr]
    assert "ChannelError: chat is down" in caplog.text
