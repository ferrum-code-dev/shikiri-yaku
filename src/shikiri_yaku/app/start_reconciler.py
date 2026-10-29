"""Settles the engine starts a previous run left unfinished, by observing the engine (§10)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from shikiri_yaku.ports import ShikiruError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from shikiri_yaku.ports import Clock, ShikiruClient, StartLog, UnsettledStart


class StartReconciler:
    """Confirms or fails the starts left unsettled by an earlier run, from `engine_up` alone.

    An unsettled start may or may not have run `shikiru start`, so this never runs it again. An
    engine seen up confirms the start; one still down `confirm_within` after it was issued fails
    it. A failed start then answers later restart requests with its history, so an uncertain start
    never leads to a second blind start. Its restart budget was already spent when it was granted.
    """

    def __init__(
        self,
        log: StartLog,
        clients: Mapping[str, ShikiruClient],
        clock: Clock,
        *,
        confirm_within: timedelta,
        interval_s: float,
    ) -> None:
        self._log = log
        self._clients = clients
        self._clock = clock
        self._confirm_within = confirm_within
        self._interval_s = interval_s

    async def run(self) -> None:
        # keep: taken first, so a start this process issues later is in flight, not uncertain
        uncertain = {start.signal.id for start in self._log.unsettled()}
        while True:
            await self.tick(uncertain)
            await self._clock.sleep(self._interval_s)

    async def tick(self, uncertain: set[str]) -> None:
        for start in self._log.unsettled():
            if start.signal.id in uncertain:
                # keep: a link outage leaves this start for the next tick
                with contextlib.suppress(ShikiruError):
                    await self._settle(start)

    async def _settle(self, start: UnsettledStart) -> None:
        now = self._clock.now()
        if (await self._clients[start.signal.project].status()).engine_up:
            self._log.confirm(start.signal, now)
        elif now >= start.since + self._confirm_within:
            self._log.fail(start.signal, now)
