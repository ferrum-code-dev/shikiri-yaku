"""The Wait action: parks a quota escalation until the reset, then retries it (design §6.2)."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outbound import TIME_FORMAT
from shikiri_yaku.domain.outcome import Forward, WaitUntil
from shikiri_yaku.extensions.infrastructure.retry import NO_ESCALATION
from shikiri_yaku.extensions.infrastructure.rules import LADDER_EXHAUSTED, QUOTA_EXHAUSTED

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime, timedelta

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.extensions.infrastructure.retry import RetryAction
    from shikiri_yaku.ports import Clock, EngineStatus, Notifier, ShikiruClient, WaitLog

WAIT_REASONS: Final = frozenset({QUOTA_EXHAUSTED, LADDER_EXHAUSTED})
EXHAUSTED_PCT: Final = 100  # keep: Shikiru's own threshold for a window that is used up
NO_RESET: Final = "quota wait not possible: no exhausted quota window names a reset time"


class WaitAction:
    """Parks a signal until the latest exhausted reset plus a margin, and tells the PO the time.

    The wait is journaled before the signal parks, so after a restart the op says whether it is
    due, and the notice is sent once per signal. When it is due, the retry is the Retry action's,
    so its loop guard and journal apply. A reset beyond the wait limit forwards at once.
    """

    reasons = WAIT_REASONS

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        retry: RetryAction,
        waits: WaitLog,
        notifier: Notifier,
        clock: Clock,
        *,
        max_wait: timedelta,
        margin: timedelta,
    ) -> None:
        self._clients = clients
        self._retry = retry
        self._waits = waits
        self._notifier = notifier
        self._clock = clock
        self._max_wait = max_wait
        self._margin = margin

    async def run(self, signal: Signal) -> Outcome:
        # keep: a retry needs the escalation, so nothing is parked for a signal without one
        if signal.escalation_id is None:
            return Forward(NO_ESCALATION)
        waited = self._waits.existing(signal)
        if waited is None:
            return await self._park(signal)
        now = self._clock.now()
        if now < waited.wake_at:
            return WaitUntil(waited.wake_at, _waiting(waited.wake_at))
        self._waits.finish(signal, now)
        return await self._retry.run(signal)

    async def _park(self, signal: Signal) -> Outcome:
        status = await self._clients[signal.project].status()
        reset = _latest_reset(status)
        now = self._clock.now()
        if reset is None:
            return Forward(NO_RESET)
        if reset > now + self._max_wait:
            return Forward(_too_late(reset))
        wake_at = reset + self._margin
        # keep: told before it is journaled, so a crash never leaves a wait the PO was not told of
        self._notifier.tell(signal, _notice(signal, reset), key=f"wait:{signal.id}")
        self._waits.record(signal, wake_at, now)
        return WaitUntil(wake_at, _waiting(wake_at))


def _latest_reset(status: EngineStatus) -> datetime | None:
    resets = [
        window.resets_at
        for windows in status.quotas.values()
        for window in windows
        if window.used_pct >= EXHAUSTED_PCT and window.resets_at is not None
    ]
    return max(resets, default=None)


def _clock_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def _notice(signal: Signal, reset: datetime) -> str:
    when = _clock_time(reset)
    return f"{signal.project}: quota is used up; the escalation is retried after {when}"


def _waiting(wake_at: datetime) -> str:
    return f"waiting until {_clock_time(wake_at)}, then the escalation is retried"


def _too_late(reset: datetime) -> str:
    return f"quota wait not possible: the reset at {_clock_time(reset)} is beyond the wait limit"
