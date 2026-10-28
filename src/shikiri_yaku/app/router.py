"""The Router: finds the handler of a signal's category and guarantees the fallback (design §6)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.outcome import Forward, Resolved, WaitUntil

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from shikiri_yaku.domain.classification import Classification, Classifier
    from shikiri_yaku.domain.extension import Handler
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.schedule import Parked
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, Forwarder, RoutingLog

_LOG = logging.getLogger(__name__)


class Router:
    """Classifies a signal, runs its handler and turns every failure into a Forward.

    Each signal runs as its own asyncio task, so a slow handler holds no other signal. The handler
    is stored with the signal, and a parked signal wakes the handler that parked it. This is the
    only place where a failure becomes a Forward; the handlers and ports let exceptions cross. A
    forward the Channel cannot take returns the signal to `new` after `forward_retry_s`.
    """

    def __init__(
        self,
        classifier: Classifier,
        handlers: Mapping[Category, Handler],
        fallback: Forwarder,
        log: RoutingLog,
        clock: Clock,
        *,
        handler_timeout_s: float,
        forward_retry_s: float,
    ) -> None:
        self._classifier = classifier
        self._handlers = handlers
        self._fallback = fallback
        self._log = log
        self._clock = clock
        self._timeout_s = handler_timeout_s
        self._retry_s = forward_retry_s
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, number: int, signal: Signal) -> asyncio.Task[None]:
        """Handle a new signal in a task of its own."""
        return self._spawn(self.route(number, signal))

    async def wake(self, parked: Parked) -> None:
        """Hand a claimed parked signal to the handler that parked it, in a task of its own."""
        self._spawn(self._resume(parked))

    async def join(self) -> None:
        """Wait until every task started so far has ended."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def route(self, number: int, signal: Signal) -> None:
        classification, failure = await self._classify(signal)
        handler = None if classification is None else self._handlers.get(classification.category)
        name = None if classification is None or handler is None else classification.category
        if not self._log.begin(number, classification, name, self._clock.now()):
            return
        if handler is None:
            outcome = Forward(failure or _unknown(classification))
        else:
            outcome = await self._run(handler, signal)
        await self._settle(number, signal, outcome, classification.note if classification else "")

    async def _resume(self, parked: Parked) -> None:
        handler = self._handlers.get(Category(parked.handler))
        if handler is None:
            outcome: Outcome = Forward(f"no handler '{parked.handler}' to wake the signal")
        else:
            outcome = await self._run(handler, parked.signal)
        suggestion = self._log.suggestion(parked.number)
        await self._settle(parked.number, parked.signal, outcome, suggestion)

    def _spawn(self, work: Coroutine[None, None, None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _classify(self, signal: Signal) -> tuple[Classification | None, str]:
        try:
            return await self._classifier.classify(signal), ""
        except Exception as error:  # noqa: BLE001 -- any failure must end as a Forward
            return None, f"classifying failed: {_describe(error)}"

    async def _run(self, handler: Handler, signal: Signal) -> Outcome:
        limit = asyncio.timeout(self._timeout_s)
        try:
            async with limit:
                return await handler.handle(signal)
        except Exception as error:  # noqa: BLE001 -- any failure must end as a Forward
            if limit.expired():
                return Forward(f"handler timed out after {self._timeout_s:g} s")
            return Forward(f"handler failed: {_describe(error)}")

    async def _settle(self, number: int, signal: Signal, outcome: Outcome, suggestion: str) -> None:
        match outcome:
            case Resolved(note):
                self._log.resolve(number, note, self._clock.now())
            case Forward(note):
                try:
                    await self._fallback.forward(number, signal, note, suggestion)
                except Exception as error:  # noqa: BLE001 -- the Channel may fail in any way
                    await self._retry_later(number, error)
                    return
                self._log.forwarded(number, note, self._clock.now())
            case WaitUntil(wake_at, note):
                self._log.park(number, wake_at, note, self._clock.now())

    async def _retry_later(self, number: int, error: Exception) -> None:
        _LOG.warning(
            "forwarding S%d failed, trying again in %g s: %s",
            number,
            self._retry_s,
            _describe(error),
        )
        await self._clock.sleep(self._retry_s)
        self._log.requeue(number, self._clock.now())


def _unknown(classification: Classification | None) -> str:
    category = "" if classification is None else classification.category
    return f"no handler is registered for category '{category}'"


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
