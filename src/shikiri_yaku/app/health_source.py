"""The health source of one project: its MCP link, unreachable for too long (design §4)."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.signal import Closing, Kind, Signal

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.ports import Clock, SignalLog

LINK_SCOPE = "link"
EVENTS_SCOPE = "events"
LIVENESS_SCOPE = "liveness"
REASONER_SCOPE = "reasoner"
RECOVERED_LINE: Final = "Recovered"
REASONER_DOWN_LINE: Final = (
    "Reasoner unavailable: every rung failed; decisions are forwarded without a suggestion"
)
OUTAGE_ID_FORMAT = "%Y-%m-%dT%H:%MZ"
SOURCE_ID_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class HealthSource:
    """Turns the link's failures and recoveries, as its owner reports them, into one signal.

    A source whose own work fails (a malformed event) is reported by scope: its signal opens at
    once, because waiting does not help, and closes when that source works again.

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
        self._failing: set[str] = set()
        self._working: set[str] = set()

    def link_failed(self) -> None:
        now = self._clock.now()
        self._reachable = False
        if self._since is None:
            self._since = now
        if now - self._since >= self._patience and not self._journal.has_open(
            self._project, Kind.HEALTH, LINK_SCOPE
        ):
            self._journal.open(self._outage(self._since), now)

    def link_recovered(self) -> None:
        if self._reachable is not True:
            closing = Closing(
                self._project, kind=Kind.HEALTH, reason=LINK_SCOPE, final_line=RECOVERED_LINE
            )
            self._journal.close(closing, self._clock.now())
        self._reachable = True
        self._since = None

    def source_failed(self, scope: str, error: Exception) -> None:
        self._failed(scope, f"The {scope} source of {self._project} fails: {error}")

    def source_recovered(self, scope: str) -> None:
        if scope not in self._working:
            closing = Closing(
                self._project, kind=Kind.HEALTH, reason=scope, final_line=RECOVERED_LINE
            )
            self._journal.close(closing, self._clock.now())
            self._working.add(scope)
        self._failing.discard(scope)

    def reasoner_failed(self) -> None:
        """Open the one signal for a call that every rung failed; later failures add none."""
        self._failed(REASONER_SCOPE, REASONER_DOWN_LINE)

    def reasoner_recovered(self) -> None:
        self.source_recovered(REASONER_SCOPE)

    def _failed(self, scope: str, text: str) -> None:
        now = self._clock.now()
        self._working.discard(scope)
        if scope in self._failing:
            return
        self._failing.add(scope)
        if not self._journal.has_open(self._project, Kind.HEALTH, scope):
            self._journal.open(self._signal(scope, now, text, SOURCE_ID_FORMAT), now)

    def _signal(self, scope: str, since: datetime, text: str, id_format: str) -> Signal:
        stamp = since.astimezone(UTC).strftime(id_format)
        return Signal(self._project, Kind.HEALTH, f"{scope}:{stamp}", scope, text)

    def _outage(self, since: datetime) -> Signal:
        text = f"The link to {self._project} has been unreachable since {since.isoformat()}."
        return self._signal(LINK_SCOPE, since, text, OUTAGE_ID_FORMAT)
