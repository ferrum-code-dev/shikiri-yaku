from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from shikiri_yaku.app.health_source import EVENTS_SCOPE, LIVENESS_SCOPE
from shikiri_yaku.ports import ShikiruError, ShikiruLinkDownError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from shikiri_yaku.app.event_source import EventSource
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.liveness_source import LivenessSource
    from shikiri_yaku.ports import Clock


class ProjectSources:
    """Long-poll events and synchronize lifecycle state before each liveness decision.

    One reader owns the cursor. Its long poll ends by the next liveness deadline, and queued
    batches are drained after a down status so a pending clean stop cannot look like a crash.
    Both sources report to the same health source; a failed sync prevents a liveness decision.
    A link failure is retried and reported by its delayed rule; any other Shikiru error opens
    a health signal of the failing source at once, and the same batch is read again after the
    delay because the cursor moves only with a committed batch.
    """

    def __init__(
        self,
        events: EventSource,
        liveness: LivenessSource,
        health: HealthSource,
        clock: Clock,
        *,
        event_wait_s: float,
        interval_s: float,
    ) -> None:
        self._events = events
        self._liveness = liveness
        self._health = health
        self._clock = clock
        self._event_wait_s = event_wait_s
        self._interval = timedelta(seconds=interval_s)
        self._retry_s = min(event_wait_s, interval_s)

    async def run(self) -> None:
        """Run until cancelled; retry failures without deciding from stale lifecycle state."""
        due = self._clock.now()
        while True:
            wait_s = min(self._event_wait_s, max(0, (due - self._clock.now()).total_seconds()))
            if not await self._guarded(EVENTS_SCOPE, self._events.poll(wait_s=wait_s)):
                continue
            if self._clock.now() >= due:
                if not await self._guarded(LIVENESS_SCOPE, self._liveness.check()):
                    continue
                due = self._clock.now() + self._interval
            self._health.link_recovered()

    async def _guarded(self, scope: str, work: Awaitable[object]) -> bool:
        try:
            await work
        except ShikiruLinkDownError:
            self._health.link_failed()
        except ShikiruError as error:
            self._health.source_failed(scope, error)
        else:
            self._health.source_recovered(scope)
            return True
        await self._clock.sleep(self._retry_s)
        return False
