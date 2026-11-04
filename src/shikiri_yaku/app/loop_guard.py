"""The loop guard: automatic actions are limited per cause, counted from the journal."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.loop_guard import AutoAction, AutoOp, Cause, Granted, Refused
from shikiri_yaku.domain.outbound import moment_text

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.ports import ActionLog, Clock

DAY: Final = timedelta(days=1)
HOUR: Final = timedelta(hours=1)
REFUSED_SUFFIX: Final = "_refused"
_UNITS: Final = {DAY: "day", HOUR: "hour"}
_NOUNS: Final = {
    AutoAction.RETRY: ("retry", "retries"),
    AutoAction.RESTART: ("restart", "restarts"),
}


class LoopGuard:
    """Grants an automatic action while its budget lasts and journals the grant before it runs.

    A refusal is journaled too, as its own kind and already final because no effect waits on it,
    so it never counts against a budget and the history can be shown. An identity carries the
    number of all earlier ops of its kind and cause, not of those in the window, so it stays
    unique after old ops leave the window.
    """

    def __init__(
        self,
        log: ActionLog,
        clock: Clock,
        retry_budget_per_day: int,
        restart_budget_per_hour: int,
    ) -> None:
        self._log = log
        self._clock = clock
        self._retry_budget = retry_budget_per_day
        self._restart_budget = restart_budget_per_hour

    def retry(self, project: str, task_id: str | None, reason: str) -> Granted | Refused:
        cause = Cause(task_id, reason)
        return self._ask(AutoAction.RETRY, project, cause, self._retry_budget, DAY)

    def restart(self, project: str) -> Granted | Refused:
        return self._ask(AutoAction.RESTART, project, None, self._restart_budget, HOUR)

    def _ask(
        self,
        action: AutoAction,
        project: str,
        cause: Cause | None,
        budget: int,
        window: timedelta,
    ) -> Granted | Refused:
        now = self._clock.now()
        used = self._log.times(action, project, cause, now - window)
        if len(used) < budget:
            return Granted(self._record(action, project, cause, None, now, refused=False))
        note = _history(action, used, budget, window, now)
        self._record(f"{action}{REFUSED_SUFFIX}", project, cause, note, now, refused=True)
        return Refused(note)

    def _record(
        self,
        kind: str,
        project: str,
        cause: Cause | None,
        payload: str | None,
        now: datetime,
        *,
        refused: bool,
    ) -> int:
        number = len(self._log.times(kind, project, cause, None)) + 1
        scope = f"{cause.task_id or ''}:{cause.reason or ''}" if cause else ""
        identity = f"{kind}:{project}:{scope}:{number}"
        return self._log.record(AutoOp(identity, kind, project, cause, payload, refused), now)


def _history(
    action: AutoAction, used: list[datetime], budget: int, window: timedelta, now: datetime
) -> str:
    noun, plural = _NOUNS[action]
    last = moment_text(max(used), now)
    allowed = "once" if budget == 1 else f"{budget} times"
    return (
        f"The last automatic {noun} was at {last}. "
        f"Automatic {plural} are limited to {allowed} per {_UNITS[window]}."
    )
