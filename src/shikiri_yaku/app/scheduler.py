"""The Scheduler: wakes parked signals and reminds the PO of open questions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from shikiri_yaku.domain.schedule import due_occurrence
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.schedule import Awaiting
    from shikiri_yaku.ports import Clock, ScheduleLog, WakeTarget


class Expiry(Protocol):
    """Ends the conversations that are due, and retries the endings not yet done."""

    async def sweep(self) -> None: ...


class Scheduler:
    """Everything it knows is in the journal, so a restart loses no wake-up and no reminder.

    A parked signal is claimed with a conditional state change before it is handed on, so it is
    dispatched once. A reminder occurrence is booked as an op and queued in the outbox in one step;
    the outbox sender delivers it. Expiry is swept on every tick.
    """

    def __init__(
        self,
        log: ScheduleLog,
        target: WakeTarget,
        clock: Clock,
        *,
        interval_s: float,
        reminder_first_h: int,
        reminder_repeat_h: int,
        expiry: Expiry | None = None,
    ) -> None:
        self._log = log
        self._target = target
        self._clock = clock
        self._interval_s = interval_s
        self._first_h = reminder_first_h
        self._repeat_h = reminder_repeat_h
        self._expiry = expiry

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        await self._wake_due()
        self._remind()
        if self._expiry is not None:
            await self._expiry.sweep()

    async def _wake_due(self) -> None:
        for parked in self._log.due_parked(self._clock.now()):
            if self._log.claim(parked.number, self._clock.now()):
                await self._target.wake(parked)

    def _remind(self) -> None:
        for awaiting in self._log.awaiting_answer():
            now = self._clock.now()
            occurrence = due_occurrence(awaiting.asked_at, now, self._first_h, self._repeat_h)
            if occurrence:
                self._log.schedule_reminder(
                    awaiting, occurrence, Message(_reminder_text(awaiting)), now
                )


def _reminder_text(awaiting: Awaiting) -> str:
    task = f" · {awaiting.task_id}" if awaiting.task_id else ""
    return f"Still waiting for your answer: S{awaiting.number} · {awaiting.project}{task}"
