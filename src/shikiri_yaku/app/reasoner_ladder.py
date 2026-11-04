"""The Reasoner's ladder: its rungs in order, a rung that failed skipped for a while.

The ladder is a wrapper behind the Reasoner port, kept in memory. At most `max_parallel` calls run
at once, and every call that finds no usable rung raises one ReasonerError with each rung's reason.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from shikiri_yaku.ports import ReasonerError, RungState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from shikiri_yaku.domain.classification import Assessment
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import AssessmentDeadline, Clock, ReasonerRung


class LadderReasoner:
    """Tries the rungs in order; a rung that failed is skipped until its cooldown has passed."""

    def __init__(
        self,
        rungs: Sequence[ReasonerRung],
        clock: Clock,
        *,
        cooldown: timedelta,
        max_parallel: int,
    ) -> None:
        self._rungs = tuple(rungs)
        self._clock = clock
        self._cooldown = cooldown
        self._slots = asyncio.Semaphore(max_parallel)
        self._failed: dict[int, tuple[datetime, str]] = {}

    async def assess_blocker(
        self,
        signal: Signal,
        host_facts: Sequence[str],
        *,
        deadline: AssessmentDeadline | None = None,
    ) -> Assessment:
        if deadline is None:
            async with self._slots:
                return await self._climb(signal, host_facts)
        async with self._slots:
            return await self._climb(signal, host_facts, deadline=deadline)

    async def _climb(
        self,
        signal: Signal,
        host_facts: Sequence[str],
        *,
        deadline: AssessmentDeadline | None = None,
    ) -> Assessment:
        reasons: list[str] = []
        for index, rung in enumerate(self._rungs):
            if self._cooling(index):
                reasons.append(f"skipped for its cooldown; last failure: {self._failed[index][1]}")
                continue
            try:
                return await rung.assess_blocker(signal, host_facts)
            except asyncio.CancelledError:
                if deadline is not None and deadline.expired():
                    reason = f"{rung.name} timed out before the assessment deadline"
                    self._failed[index] = (self._clock.now() + self._cooldown, reason)
                raise
            except ReasonerError as error:
                reason = str(error)
                self._failed[index] = (self._clock.now() + self._cooldown, reason)
                reasons.append(reason)
        raise ReasonerError("; ".join(reasons))

    def rung_states(self) -> Sequence[RungState]:
        return [self._state(index, rung) for index, rung in enumerate(self._rungs)]

    def _state(self, index: int, rung: ReasonerRung) -> RungState:
        failure = self._failed.get(index)
        if failure is None:
            return RungState(rung.name, last_failure=None, cooling_until=None)
        until, reason = failure
        cooling = until if self._clock.now() < until else None
        return RungState(rung.name, last_failure=reason, cooling_until=cooling)

    def _cooling(self, index: int) -> bool:
        failure = self._failed.get(index)
        return failure is not None and self._clock.now() < failure[0]
