"""The Retry action: asks the loop guard, then gives the escalation back to Shikiru (§6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.loop_guard import Refused
from shikiri_yaku.domain.outcome import Forward, Resolved
from shikiri_yaku.extensions.infrastructure.rules import (
    AGENT_ERROR,
    AGENT_PROTOCOL,
    AGENT_STUCK,
    INTEGRATION_BLOCKED,
    INTEGRATION_CONFLICT,
)
from shikiri_yaku.ports import ResolutionKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Guard, ShikiruClient

RETRY_REASONS: Final = frozenset(
    {AGENT_ERROR, AGENT_STUCK, AGENT_PROTOCOL, INTEGRATION_CONFLICT, INTEGRATION_BLOCKED}
)
RETRIED: Final = "retried once; Shikiru takes the task back"
NO_ESCALATION: Final = "nothing to retry: the signal names no escalation"


class RetryAction:
    """Gives an escalation back to Shikiru at once, when the loop guard grants the retry.

    Shikiru has already backed off before it escalates, so no wait is added here. A refused guard
    forwards with its history, and a rejected resolution fails in the client, which the Router
    turns into a Forward.
    """

    reasons = RETRY_REASONS

    def __init__(self, clients: Mapping[str, ShikiruClient], guard: Guard) -> None:
        self._clients = clients
        self._guard = guard

    async def run(self, signal: Signal) -> Outcome:
        # keep: checked before the guard, so a signal with no escalation spends no retry
        if signal.escalation_id is None:
            return Forward(NO_ESCALATION)
        decision = self._guard.retry(signal.project, signal.task_id, signal.reason)
        if isinstance(decision, Refused):
            return Forward(decision.note)
        client = self._clients[signal.project]
        await client.resolve_escalation(signal.escalation_id, ResolutionKind.RETRY)
        return Resolved(RETRIED)
