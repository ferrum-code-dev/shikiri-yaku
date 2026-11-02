"""The notice handler: tells the PO what finished or failed, and asks nothing (design §6.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import STOP_REMINDERS_LABEL, Purpose, fit
from shikiri_yaku.domain.outcome import Forward, Informed, Notified, Resolved
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.notice.family import family_of
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Button, Message

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        Notifier,
        Outbox,
        ShikiruClient,
        SignalNumbers,
    )

NO_TASK: Final = "nothing, the task of the signal is not on record"
WAITING: Final = "waiting, a task of its family is still open"
SENT: Final = "sent the notice to the PO"
INTEGRATED_KEY: Final = "integrated"
HEALTH_KEY: Final = "health"


class NoticeHandler:
    """Sends the notice of a finished family once per root, and the message of a health signal.

    The last task of a family to finish speaks for the whole family, so the earlier ones stay
    silent. A health message carries its signal number, so the final edit that says "Recovered"
    finds it the way it finds a review message.
    """

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        numbers: SignalNumbers,
        outbox: Outbox,
        notifier: Notifier,
        clock: Clock,
        *,
        destinations: Destinations,
    ) -> None:
        self._clients = clients
        self._numbers = numbers
        self._outbox = outbox
        self._notifier = notifier
        self._clock = clock
        self._destinations = destinations

    async def handle(self, signal: Signal) -> Outcome:
        if signal.kind is Kind.HEALTH:
            return self._health(signal)
        return await self._task_done(signal)

    async def _task_done(self, signal: Signal) -> Outcome:
        if signal.task_id is None:
            return Forward(NO_TASK)
        tasks = await self._clients[signal.project].list_tasks()
        family = family_of(tasks, signal.task_id)
        if family is None:
            message = f"task {signal.task_id} is not in Shikiru's task list"
            raise LookupError(message)
        if not family.settled:
            return Resolved(WAITING)
        # keep: a notice in its component's topic already names the component, so it is left out
        prefix = "" if self._in_component_topic(signal) else f"{signal.project} · "
        text = f"{prefix}{family.root.title} integrated ({_tasks(len(family.members))})"
        self._notifier.tell(
            signal,
            fit(text, MESSAGE_TEXT_LIMIT),
            key=f"{INTEGRATED_KEY}:{signal.project}:{family.root.id}",
        )
        return Informed(SENT)

    def _in_component_topic(self, signal: Signal) -> bool:
        destinations = self._destinations.for_signal(signal.project, signal.kind)
        return all(
            self._destinations.component_of(destination.thread) == signal.project
            for destination in destinations
        )

    def _health(self, signal: Signal) -> Outcome:
        number = self._numbers.number_of(signal)
        if number is None:
            message = f"{signal.id} is not in the journal"
            raise LookupError(message)
        self._outbox.queue(
            number,
            Purpose.NOTICE,
            self._destinations.for_signal(signal.project, signal.kind),
            Message(
                fit(signal.text, MESSAGE_TEXT_LIMIT),
                (Button(STOP_REMINDERS_LABEL, stop_reminders_data(number)),),
            ),
            key=f"{HEALTH_KEY}:{number}",
            now=self._clock.now(),
        )
        return Notified(SENT)


def _tasks(count: int) -> str:
    return f"{count} task" if count == 1 else f"{count} tasks"
