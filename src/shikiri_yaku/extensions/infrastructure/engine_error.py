"""The engine_error action: restarts a down engine if needed, then gives the escalation back."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.extensions.infrastructure.rules import ENGINE_ERROR

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.extensions.infrastructure.restart import RestartAction
    from shikiri_yaku.extensions.infrastructure.retry import RetryAction
    from shikiri_yaku.ports import ShikiruClient

ENGINE_ERROR_REASONS: Final = frozenset({ENGINE_ERROR})


class EngineErrorAction:
    """Restarts the engine only while it is down, then retries the escalation.

    An engine that is up is never started, so the escalation is only given back. A restart that
    forwards leaves the escalation with the PO, so it is not given back.
    """

    reasons = ENGINE_ERROR_REASONS

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        restart: RestartAction,
        retry: RetryAction,
    ) -> None:
        self._clients = clients
        self._restart = restart
        self._retry = retry

    async def run(self, signal: Signal) -> Outcome:
        if not (await self._clients[signal.project].status()).engine_up:
            restarted = await self._restart.run(signal)
            if isinstance(restarted, Forward):
                return restarted
        return await self._retry.run(signal)
