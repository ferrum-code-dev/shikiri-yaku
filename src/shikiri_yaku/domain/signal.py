"""A signal: one thing that may need attention, whatever source it came from (design §4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    ESCALATION = "escalation"
    ENGINE_DOWN = "engine_down"
    ENGINE_FAULT = "engine_fault"
    TASK_DONE = "task_done"
    PR_REVIEW = "pr_review"
    HEALTH = "health"


@dataclass(frozen=True, slots=True)
class Signal:
    project: str
    kind: Kind
    source_id: str
    reason: str
    text: str
    task_id: str | None = None
    escalation_id: str | None = None

    @property
    def id(self) -> str:
        return f"{self.project}:{self.kind.value}:{self.source_id}"

    @property
    def reason_key(self) -> str:
        return self.reason if self.kind is Kind.ESCALATION else self.kind.value
