"""The Reasoner's ladder: its rungs in order, a rung that failed skipped for a while (design §8).

The ladder is a wrapper behind the Reasoner port, kept in memory. At most `max_parallel` calls run
at once, and every call that finds no usable rung raises one ReasonerError with each rung's reason.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from shikiri_yaku.ports import ReasonerError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from shikiri_yaku.domain.classification import Assessment
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, ReasonerRung


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

    async def assess_blocker(self, signal: Signal, host_facts: Sequence[str]) -> Assessment:
        async with self._slots:
            return await self._climb(signal, host_facts)

    async def _climb(self, signal: Signal, host_facts: Sequence[str]) -> Assessment:
        reasons: list[str] = []
        for index, rung in enumerate(self._rungs):
            if self._cooling(index):
                reasons.append(f"skipped for its cooldown; last failure: {self._failed[index][1]}")
                continue
            try:
                return await rung.assess_blocker(signal, host_facts)
            except ReasonerError as error:
                reason = str(error)
                self._failed[index] = (self._clock.now() + self._cooldown, reason)
                reasons.append(reason)
        raise ReasonerError("; ".join(reasons))

    def _cooling(self, index: int) -> bool:
        failure = self._failed.get(index)
        return failure is not None and self._clock.now() < failure[0]
