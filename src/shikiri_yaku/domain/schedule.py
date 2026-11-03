"""What the Scheduler hands on: a woken signal and a message nobody has answered."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class Parked:
    """A signal whose wake-up time has come, with the handler that parked it."""

    number: int
    handler: str
    signal: Signal


@dataclass(frozen=True, slots=True)
class Destination:
    channel: str
    chat: str
    thread: int | None = None


@dataclass(frozen=True, slots=True)
class Awaiting:
    """A forwarded signal the PO has not answered, asked first at `asked_at`."""

    number: int
    project: str
    task_id: str | None
    asked_at: datetime
    destinations: tuple[Destination, ...]


def due_occurrence(asked_at: datetime, now: datetime, first_h: int, repeat_h: int) -> int:
    """Return the newest reminder due at `now`: 1 after `first_h`, then one per `repeat_h`.

    After a long stop only the newest occurrence counts, so the PO gets one reminder, not a burst.
    """
    waited = now - asked_at - timedelta(hours=first_h)
    if waited < timedelta(0):
        return 0
    return 1 + waited // timedelta(hours=repeat_h)
