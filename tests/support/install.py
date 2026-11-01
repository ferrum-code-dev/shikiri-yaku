from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outcome import Notified

if TYPE_CHECKING:
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal


class UnusedInstallAction:
    """An empty action for infrastructure tests that do not exercise host-tool routing."""

    reasons: frozenset[str] = frozenset()

    async def run(self, signal: Signal) -> Outcome:
        del signal
        return Notified("unused install action")
