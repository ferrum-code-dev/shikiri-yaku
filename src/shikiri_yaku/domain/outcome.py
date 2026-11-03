"""What a handler returns for one signal."""

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
    """The signal goes to the PO; `standing` says its message is about something that stays true.

    A standing message keeps Stop reminders. The handler decides, not the kind of the
    signal, so a new notice is informational unless its handler says otherwise.
    """

    note: str
    standing: bool = False


@dataclass(frozen=True, slots=True)
class WaitUntil:
    wake_at: datetime
    note: str


@dataclass(frozen=True, slots=True)
class Informed:
    """The handler sent the PO a message that only informs; the signal closes when it is sent.

    Such a message has no button, so it never gets a reminder; its message is the whole answer.
    """

    note: str


@dataclass(frozen=True, slots=True)
class Notified:
    """The handler sent the PO its own message about the signal, which then waits as a forward does.

    Reminders apply to it as to a forward. The Review handler can send its own message without also
    sending the Product message.
    """

    note: str


type Outcome = Resolved | Forward | WaitUntil | Notified | Informed
