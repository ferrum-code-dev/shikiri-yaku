"""The liveness source of one project: a crashed engine raises no escalation."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outbound import ENGINE_UP_LINE
from shikiri_yaku.domain.signal import Closing, Kind, Signal
from shikiri_yaku.ports import EventKinds

if TYPE_CHECKING:
    from shikiri_yaku.app.event_source import EventSource
    from shikiri_yaku.domain.signal import Lifecycle, LifecycleState
    from shikiri_yaku.ports import Clock, LivenessJournal, ShikiruClient

_ENGINE_KINDS: Final = (Kind.ENGINE_DOWN, Kind.ENGINE_FAULT)


class LivenessSource:
    """Reads `status.engine_up` and applies the engine-liveness rules.

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
        *,
        stop_grace_min: int,
        events: EventSource,
    ) -> None:
        self._events = events
        self._project = project
        self._client = client
        self._journal = journal
        self._clock = clock
        self._grace = timedelta(minutes=stop_grace_min)
        self._run_started = clock.now()

    async def check(self) -> None:
        if (await self._client.status()).engine_up:
            self._close_engine_signals()
            return
        while await self._events.poll(wait_s=0):
            pass
        state = self._journal.state(self._project)
        if state is None or state.lifecycle is None:
            return
        lifecycle = state.lifecycle
        if lifecycle.kind == EventKinds.ENGINE_STARTED:
            self._engine_is_down(state, lifecycle)
        elif not lifecycle.has_reason and lifecycle.event_id != state.respected_stop_id:
            self._clean_stop(state, lifecycle)

    def _close_engine_signals(self) -> None:
        # keep: only what the PO was shown closes; a parked or handled signal is its handler's
        now = self._clock.now()
        for kind in _ENGINE_KINDS:
            closing = Closing(self._project, kind, forwarded_only=True, final_line=ENGINE_UP_LINE)
            self._journal.close(closing, now)

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
            f"The engine of {self._project} is down.",
        )
        self._journal.open(signal, self._clock.now())
