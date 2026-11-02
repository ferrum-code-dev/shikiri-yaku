"""What a handler returns for one signal (design §6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class Resolved:
    note: str


@dataclass(frozen=True, slots=True)
class Forward:
    note: str


@dataclass(frozen=True, slots=True)
class WaitUntil:
    wake_at: datetime
    note: str


@dataclass(frozen=True, slots=True)
class Informed:
    """The handler sent the PO a message that only informs; the signal closes when it is sent.

    Such a message has no button, so it never gets a reminder (design §6.1). This outcome is not in
    design §6: the handler's message is the whole answer (tasks/plan.md E10).
    """

    note: str


@dataclass(frozen=True, slots=True)
class Notified:
    """The handler sent the PO its own message about the signal, which then waits as a forward does.

    Reminders apply to it as to a forward. This outcome is not in design §6: the review handler
    needs it, because a forward would send the Product message (tasks/plan.md C4).
    """

    note: str


type Outcome = Resolved | Forward | WaitUntil | Notified | Informed
