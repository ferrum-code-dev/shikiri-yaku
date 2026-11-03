"""What the loop guard decides about an automatic action."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutoAction(StrEnum):
    RETRY = "retry"
    RESTART = "restart"


@dataclass(frozen=True, slots=True)
class Cause:
    """What a budget is counted per; a restart has no cause, it is counted per project."""

    task_id: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class AutoOp:
    """An automatic action about to happen, as the journal keeps it."""

    identity: str
    kind: str
    project: str
    cause: Cause | None
    payload: str | None
    refused: bool = False


@dataclass(frozen=True, slots=True)
class Granted:
    """The action may run; `op_id` is its journaled op, still in the state `intended`."""

    op_id: int


@dataclass(frozen=True, slots=True)
class Refused:
    """The budget is spent; `note` carries the history for the PO."""

    note: str
