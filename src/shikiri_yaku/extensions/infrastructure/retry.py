"""The Retry action: asks the loop guard, journals the retry, then returns the escalation (§6.2)."""

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
from shikiri_yaku.ports import (
    ResolutionKind,
    RetryAttemptState,
    ShikiruRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, Guard, RetryLog, ShikiruClient

RETRY_REASONS: Final = frozenset(
    {AGENT_ERROR, AGENT_STUCK, AGENT_PROTOCOL, INTEGRATION_CONFLICT, INTEGRATION_BLOCKED}
)
RETRIED: Final = "retried once; Shikiru takes the task back"
NO_ESCALATION: Final = "nothing to retry: the signal names no escalation"
ALREADY_RETRIED: Final = "nothing to retry: this escalation was already given back to Shikiru"
RETRY_FAILED: Final = "the automatic retry did not complete; please decide"


class RetryAction:
    """Gives an escalation back to Shikiru at once, when the loop guard grants the retry.

    The retry is journaled as issued before Shikiru is called, and its acknowledgement is kept.
    The events then confirm it, and the reconciler settles it if it is queued and lost, or never
    sent. Shikiru has already backed off before it escalates, so no wait is added here. A refused
    guard forwards with its history. A rejected resolution is failed in the journal and re-raised,
    so the Router forwards it.
    """

    reasons = RETRY_REASONS

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        guard: Guard,
        retries: RetryLog,
        clock: Clock,
    ) -> None:
        self._clients = clients
        self._guard = guard
        self._retries = retries
        self._clock = clock

    async def run(self, signal: Signal) -> Outcome:
        # keep: checked before the guard, so a signal with no escalation spends no retry
        if signal.escalation_id is None:
            return Forward(NO_ESCALATION)
        existing = self._retries.existing(signal)
        if existing is not None:
            return _previous_attempt(existing.state)
        decision = self._guard.retry(signal.project, signal.task_id, signal.reason)
        if isinstance(decision, Refused):
            return Forward(decision.note)
        op_id = self._retries.issue(signal, signal.escalation_id, self._clock.now())
        if op_id is None:
            existing = self._retries.existing(signal)
            return (
                Forward(ALREADY_RETRIED) if existing is None else _previous_attempt(existing.state)
            )
        try:
            ack = await self._clients[signal.project].resolve_escalation(
                signal.escalation_id, ResolutionKind.RETRY
            )
        except ShikiruRejectedError:
            self._retries.reject(op_id, self._clock.now())
            raise
        self._retries.acknowledge(op_id, ack, self._clock.now())
        return Resolved(RETRIED)


def _previous_attempt(state: str) -> Outcome:
    if state == RetryAttemptState.FAILED:
        return Forward(RETRY_FAILED)
    return Resolved(RETRIED)
