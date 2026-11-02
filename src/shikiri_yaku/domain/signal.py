"""A signal: one thing that may need attention, whatever source it came from (design §4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime

# keep: scope of the Operator's own health signals (design §4); they belong to no component
OPERATOR_SCOPE: Final = "operator"


class Kind(StrEnum):
    ESCALATION = "escalation"
    ENGINE_DOWN = "engine_down"
    ENGINE_FAULT = "engine_fault"
    TASK_DONE = "task_done"
    PR_REVIEW = "pr_review"
    HEALTH = "health"


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The pull request a task opened for review: its link and its commit count (design §4)."""

    url: str
    commits: int


@dataclass(frozen=True, slots=True)
class Signal:
    """`reason` matters for escalations only; the other kinds are sorted by `kind` (design §5)."""

    project: str
    kind: Kind
    source_id: str
    reason: str
    text: str
    task_id: str | None = None
    escalation_id: str | None = None
    pull_request: PullRequest | None = None

    @property
    def id(self) -> str:
        return f"{self.project}:{self.kind.value}:{self.source_id}"


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """The newest `engine_started` or `engine_stopped` event of a project."""

    event_id: int
    kind: str
    has_reason: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class LifecycleState:
    """What the liveness rule reads about a project: where its events end and its newest stop."""

    last_event_id: int
    lifecycle: Lifecycle | None
    respected_stop_id: int | None


@dataclass(frozen=True, slots=True)
class Closing:
    """Closes the open signals that match every field given: what a source learned has ended.

    `final_line`, when given, is the line the closed signals' messages end with.
    `forwarded_only` leaves alone the signals the PO has not been shown yet or that are being
    handled: a source that learns the cause is gone may only close what the PO was told about.
    """

    project: str
    kind: Kind | None = None
    escalation_id: str | None = None
    task_id: str | None = None
    reason: str | None = None
    final_line: str | None = None
    forwarded_only: bool = False


@dataclass(frozen=True, slots=True)
class Resolved:
    """Shikiru's event log shows an escalation resolved; `command_id` names the command, if any."""

    project: str
    escalation_id: str
    resolution: str
    command_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskCancelled:
    """Shikiru's event log shows a task cancelled: its installs that have not run are voided."""

    project: str
    task_id: str
