"""Settles the engine starts a previous run left unfinished, by observing the engine (§10)."""

from __future__ import annotations

import asyncio
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
    A start of a component no longer configured is left as it is: neither settled nor waited for.

    `settled` is set once every project's link is up and no such start is pending, or once
    `confirm_within` has passed since this run began, so a link that stays down cannot hold the
    other components back. Signals and sources wait for it: the links connect first, and their
    handlers must not act on an unsettled start (design §10).
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
        self.settled = asyncio.Event()

    async def run(self) -> None:
        # keep: taken first, so a start this process issues later is in flight, not uncertain
        uncertain = {start.signal.id for start in self._watched()}
        deadline = self._clock.now() + self._confirm_within
        while True:
            await self.tick(uncertain)
            if self._ready(uncertain) or self._clock.now() >= deadline:
                self.settled.set()
            await self._clock.sleep(self._interval_s)

    async def tick(self, uncertain: set[str]) -> None:
        for start in self._watched():
            if start.signal.id in uncertain:
                # keep: a link outage leaves this start for the next tick
                with contextlib.suppress(ShikiruError):
                    await self._settle(start)

    def _ready(self, uncertain: set[str]) -> bool:
        linked = all(client.linked for client in self._clients.values())
        return linked and not self._pending(uncertain)

    def _pending(self, uncertain: set[str]) -> bool:
        return any(start.signal.id in uncertain for start in self._watched())

    def _watched(self) -> list[UnsettledStart]:
        return [start for start in self._log.unsettled() if start.signal.project in self._clients]

    async def _settle(self, start: UnsettledStart) -> None:
        now = self._clock.now()
        if (await self._clients[start.signal.project].status()).engine_up:
            self._log.confirm(start.signal, now)
        elif now >= start.since + self._confirm_within:
            self._log.fail(start.signal, now)
