"""The Scheduler: wakes parked signals and reminds the PO of open questions (design §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.schedule import due_occurrence
from shikiri_yaku.ports import ChannelError, Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.schedule import Awaiting
    from shikiri_yaku.ports import Channel, Clock, ScheduleLog, WakeTarget


class Scheduler:
    """Everything it knows is in the journal, so a restart loses no wake-up and no reminder.

    A parked signal is claimed with a conditional state change before it is handed on, so it is
    dispatched once. A reminder occurrence is booked as an op before it is queued in the outbox.
    If the send is lost the outbox may deliver a recognizable duplicate (design §7).
    """

    def __init__(
        self,
        log: ScheduleLog,
        target: WakeTarget,
        channel: Channel,
        clock: Clock,
        *,
        interval_s: float,
        reminder_first_h: int,
        reminder_repeat_h: int,
    ) -> None:
        self._log = log
        self._target = target
        self._channel = channel
        self._clock = clock
        self._interval_s = interval_s
        self._first_h = reminder_first_h
        self._repeat_h = reminder_repeat_h

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        await self._wake_due()
        await self._remind()

    async def _wake_due(self) -> None:
        for parked in self._log.due_parked(self._clock.now()):
            if self._log.claim(parked.number, self._clock.now()):
                await self._target.wake(parked)

    async def _remind(self) -> None:
        for awaiting in self._log.awaiting_answer():
            now = self._clock.now()
            occurrence = due_occurrence(awaiting.asked_at, now, self._first_h, self._repeat_h)
            if occurrence:
                await self._send(awaiting, occurrence)

    async def _send(self, awaiting: Awaiting, occurrence: int) -> None:
        text = _reminder_text(awaiting)
        for outgoing in self._log.schedule_reminder(awaiting, occurrence, self._clock.now()):
            try:
                ref = await self._channel.send(outgoing.destination.chat, Message(text))
            except ChannelError:
                continue
            self._log.delivered(outgoing, ref)


def _reminder_text(awaiting: Awaiting) -> str:
    task = f" · {awaiting.task_id}" if awaiting.task_id else ""
    return f"Still waiting for your answer: S{awaiting.number} · {awaiting.project}{task}"
