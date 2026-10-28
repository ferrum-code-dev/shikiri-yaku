from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from shikiri_yaku.ports import ShikiruLinkDownError

if TYPE_CHECKING:
    from shikiri_yaku.app.event_source import EventSource
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.liveness_source import LivenessSource
    from shikiri_yaku.ports import Clock


class ProjectSources:
    """Long-poll events and synchronize lifecycle state before each liveness decision.

    One reader owns the cursor. Its long poll ends by the next liveness deadline, and queued
    batches are drained after a down status so a pending clean stop cannot look like a crash.
    Both sources report to the same health source; a failed sync prevents a liveness decision.
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
        """Run until cancelled; retry link failures without deciding from stale lifecycle state."""
        due = self._clock.now()
        while True:
            wait_s = min(self._event_wait_s, max(0, (due - self._clock.now()).total_seconds()))
            try:
                await self._events.poll(wait_s=wait_s)
                if self._clock.now() >= due:
                    await self._liveness.check()
                    due = self._clock.now() + self._interval
            except ShikiruLinkDownError:
                self._health.link_failed()
                await self._clock.sleep(self._retry_s)
            else:
                self._health.link_recovered()
