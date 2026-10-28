"""The health source of one project: its MCP link, unreachable for too long (design §4)."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from shikiri_yaku.domain.signal import Closing, Kind, Signal

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.ports import Clock, SignalLog

LINK_SCOPE = "link"
OUTAGE_ID_FORMAT = "%Y-%m-%dT%H:%MZ"


class HealthSource:
    """Turns the link's failures and recoveries, as its owner reports them, into one signal.

    One `health` signal opens when a failure has lasted `link_unreachable_min` and stays the one
    signal of that outage; a recovery closes it. The journal is asked before opening, so an outage
    that an earlier run signaled is not signaled again, and the first report after a start that
    says the link works also closes, because that run may not have seen the recovery.
    """

    def __init__(
        self, project: str, journal: SignalLog, clock: Clock, link_unreachable_min: int
    ) -> None:
        self._project = project
        self._journal = journal
        self._clock = clock
        self._patience = timedelta(minutes=link_unreachable_min)
        self._since: datetime | None = None
        self._reachable: bool | None = None

    def link_failed(self) -> None:
        now = self._clock.now()
        self._reachable = False
        if self._since is None:
            self._since = now
        if now - self._since >= self._patience and not self._journal.has_open(
            self._project, Kind.HEALTH
        ):
            self._journal.open(self._outage(self._since), now)

    def link_recovered(self) -> None:
        if self._reachable is not True:
            self._journal.close(Closing(self._project, kind=Kind.HEALTH), self._clock.now())
        self._reachable = True
        self._since = None

    def _outage(self, since: datetime) -> Signal:
        return Signal(
            self._project,
            Kind.HEALTH,
            f"{LINK_SCOPE}:{since.astimezone(UTC).strftime(OUTAGE_ID_FORMAT)}",
            LINK_SCOPE,
            f"The link to {self._project} has been unreachable since {since.isoformat()}.",
        )
