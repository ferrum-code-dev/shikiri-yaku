"""The liveness source of one project: a crashed engine raises no escalation (design §4)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.ports import EventKinds, ShikiruLinkDownError

if TYPE_CHECKING:
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.domain.signal import Lifecycle, LifecycleState
    from shikiri_yaku.ports import Clock, LivenessJournal, ShikiruClient


class LivenessSource:
    """Reads `status.engine_up` and applies the table of design §4.

    A clean stop is told from a host shutdown by time: it is respected when the engine is still
    down `stop_grace_min` after a stop that happened in this run, and a clean stop from before
    this run that was never respected counts as `engine_down` at once.
    """

    def __init__(
        self,
        project: str,
        client: ShikiruClient,
        journal: LivenessJournal,
        clock: Clock,
        health: HealthSource,
        *,
        interval_s: float,
        stop_grace_min: int,
    ) -> None:
        self._project = project
        self._client = client
        self._journal = journal
        self._clock = clock
        self._health = health
        self._interval_s = interval_s
        self._grace = timedelta(minutes=stop_grace_min)
        self._run_started = clock.now()

    async def run(self) -> None:
        """Check at once and then every interval; a down link is the health source's business."""
        while True:
            try:
                await self.check()
            except ShikiruLinkDownError:
                self._health.link_failed()
            else:
                self._health.link_recovered()
            await self._clock.sleep(self._interval_s)

    async def check(self) -> None:
        if (await self._client.status()).engine_up:
            return
        state = self._journal.state(self._project)
        if state is None or state.lifecycle is None:
            return
        lifecycle = state.lifecycle
        if lifecycle.kind == EventKinds.ENGINE_STARTED:
            self._engine_is_down(state, lifecycle)
        elif not lifecycle.has_reason and lifecycle.event_id != state.respected_stop_id:
            self._clean_stop(state, lifecycle)

    def _clean_stop(self, state: LifecycleState, lifecycle: Lifecycle) -> None:
        now = self._clock.now()
        if lifecycle.at < self._run_started:
            self._engine_is_down(state, lifecycle)
        elif now - lifecycle.at >= self._grace:
            self._journal.respect_stop(self._project, lifecycle.event_id)

    def _engine_is_down(self, state: LifecycleState, lifecycle: Lifecycle) -> None:
        signal = Signal(
            self._project,
            Kind.ENGINE_DOWN,
            str(state.last_event_id),
            lifecycle.kind,
            f"The engine of {self._project} is down; its last event was {lifecycle.kind}.",
        )
        self._journal.open(signal, self._clock.now())
