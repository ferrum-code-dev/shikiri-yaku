"""The notice handler: tells the PO what finished or failed, and asks nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import STOP_REMINDERS_LABEL, Purpose, fit
from shikiri_yaku.domain.outcome import Forward, Informed, Notified, Resolved
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.notice.family import family_of
from shikiri_yaku.ports import Button, Message, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.extensions.notice.family import Family
    from shikiri_yaku.ports import (
        Clock,
        Notifier,
        Outbox,
        Places,
        ShikiruClient,
        ShikiruTask,
        SignalNumbers,
    )

NO_TASK: Final = "nothing, the task of the signal is not on record"
WAITING: Final = "waiting, a task of its family is still open"
SENT: Final = "sent the notice to the PO"
INTEGRATED_KEY: Final = "integrated"
HEALTH_KEY: Final = "health"
_DONE_MARK: Final = "✅"
_CANCELLED_MARK: Final = "❌"
_OTHER_MARK: Final = "❓"
_WARNING_MARK: Final = "⚠️"


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
        places: Places,
        tasks_max: int,
        max_text: int,
    ) -> None:
        self._clients = clients
        self._max_text = max_text
        self._numbers = numbers
        self._outbox = outbox
        self._notifier = notifier
        self._clock = clock
        self._places = places
        self._tasks_max = tasks_max

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
        # keep: a notice in its component's place already names the component, so it is left out
        prefix = "" if self._in_component_place(signal) else f"{signal.project} · "
        text = _notice_text(prefix, family, self._tasks_max, self._max_text)
        self._notifier.tell(
            signal,
            fit(text, self._max_text),
            key=f"{INTEGRATED_KEY}:{signal.project}:{family.root.id}",
        )
        return Informed(SENT)

    def _in_component_place(self, signal: Signal) -> bool:
        places = self._places.for_signal(signal.project, signal.kind)
        return all(self._places.component_of(place) == signal.project for place in places)

    def _health(self, signal: Signal) -> Outcome:
        number = self._numbers.number_of(signal)
        if number is None:
            message = f"{signal.id} is not in the journal"
            raise LookupError(message)
        self._outbox.queue(
            number,
            Purpose.NOTICE,
            self._places.for_signal(signal.project, signal.kind),
            Message(
                fit(signal.text, self._max_text),
                (Button(STOP_REMINDERS_LABEL, stop_reminders_data(number)),),
            ),
            key=f"{HEALTH_KEY}:{number}",
            now=self._clock.now(),
            standing=True,
        )
        return Notified(SENT)


def _tasks(count: int) -> str:
    return f"{count} task" if count == 1 else f"{count} tasks"


def _status_mark(status: str) -> str:
    if status == TaskStatus.DONE:
        return _DONE_MARK
    if status == TaskStatus.CANCELLED:
        return _CANCELLED_MARK
    return _OTHER_MARK


def _task_line(task: ShikiruTask) -> str:
    line = f"{_status_mark(task.status)} {task.id} {task.title}"
    if task.status != TaskStatus.DONE:
        line += f" ({task.status})"
    return line


def _notice_text(prefix: str, family: Family, tasks_max: int, max_text: int) -> str:
    integrated = sum(task.status == TaskStatus.DONE for task in family.members)
    total = len(family.members)
    count_suffix = f" — {integrated} of {_tasks(total)} integrated"
    warning = f"{_WARNING_MARK} " if integrated != total else ""
    omission = f"\n+{total} more"
    title_room = max_text - len(prefix) - len(warning) - len(count_suffix) - len(omission)
    title = fit(family.root.title, title_room) if title_room > 0 else ""
    headline = f"{warning}{prefix}{title}{count_suffix}"
    lines = [_task_line(task) for task in family.members]
    shown = lines[:tasks_max]
    remaining = total - len(shown)
    while True:
        parts = [headline, *shown]
        if remaining:
            parts.append(f"+{remaining} more")
        text = "\n".join(parts)
        if len(text) <= max_text or not shown:
            return text
        shown = shown[:-1]
        remaining += 1
