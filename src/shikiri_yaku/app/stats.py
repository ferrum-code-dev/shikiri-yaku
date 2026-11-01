"""The figures of `stats` (design §15): what the journal supports, before any output is chosen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import CompletedEscalation, StatsLog

ABSENT: Final = "absent"


@dataclass(frozen=True, slots=True)
class Measures:
    """The figures of one journal. `share` is None when the completed lifetimes sum to no time."""

    completed: int
    queued: int
    lifetime: timedelta
    waiting: timedelta
    share: float | None
    handled_without_po: Mapping[str, int]
    reasons: Mapping[str, int]
    verdicts: Mapping[str, int]
    installs: Mapping[str, int]
    install_outcomes: Mapping[str, int]


class Stats:
    """Turns the journal's counts and times into measures; the journal stores what it is given."""

    def __init__(self, log: StatsLog) -> None:
        self._log = log

    def measure(self) -> Measures:
        escalations = self._log.completed_escalations()
        lifetime = sum((item.final_at - item.created_at for item in escalations), timedelta())
        waiting = sum((_waiting(item) for item in escalations), timedelta())
        return Measures(
            completed=len(escalations),
            queued=sum(item.first_queued_at is not None for item in escalations),
            lifetime=lifetime,
            waiting=waiting,
            share=waiting / lifetime if lifetime > timedelta() else None,
            handled_without_po=_labelled(self._log.handled_without_po()),
            reasons=dict(self._log.escalation_reasons()),
            verdicts=_labelled(self._log.escalation_verdicts()),
            installs=dict(self._log.install_states()),
            install_outcomes=dict(self._log.install_outcomes()),
        )


def _waiting(item: CompletedEscalation) -> timedelta:
    if item.first_queued_at is None:
        return timedelta()
    # keep: a question queued after the last update waited for nothing, not for negative time
    return max(item.final_at - item.first_queued_at, timedelta())


def _labelled(counts: Mapping[str | None, int]) -> dict[str, int]:
    return {ABSENT if key is None else key: count for key, count in counts.items()}
