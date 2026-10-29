"""The engine_error action: restarts a down engine if needed, then gives the escalation back."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.extensions.infrastructure.rules import ENGINE_ERROR
from shikiri_yaku.ports import AttemptState

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.extensions.infrastructure.restart import RestartAction
    from shikiri_yaku.extensions.infrastructure.retry import RetryAction
    from shikiri_yaku.ports import ShikiruClient, StartLog

ENGINE_ERROR_REASONS: Final = frozenset({ENGINE_ERROR})


class EngineErrorAction:
    """Finishes a restart or starts a down engine, then retries the escalation.

    A restart already journaled for the escalation is finished without a start, so a crash after
    the start leaves one notice, not none. An engine that is up and has no such restart is never
    started; the escalation is only given back. A restart that forwards leaves the escalation with
    the PO, so it is not given back.
    """

    reasons = ENGINE_ERROR_REASONS

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        restart: RestartAction,
        retry: RetryAction,
        starts: StartLog,
    ) -> None:
        self._clients = clients
        self._restart = restart
        self._retry = retry
        self._starts = starts

    async def run(self, signal: Signal) -> Outcome:
        if await self._restart_needed(signal):
            restarted = await self._restart.run(signal)
            if isinstance(restarted, Forward):
                return restarted
        return await self._retry.run(signal)

    async def _restart_needed(self, signal: Signal) -> bool:
        attempt = self._starts.existing(signal)
        if attempt is not None and attempt.state != AttemptState.FAILED:
            return True
        return not (await self._clients[signal.project].status()).engine_up
