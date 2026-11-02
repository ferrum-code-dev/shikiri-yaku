"""The review handler: tells the PO a pull request is ready for review (design §6.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.outcome import Forward, Notified
from shikiri_yaku.extensions.review.message import review_message
from shikiri_yaku.ports import ShikiruError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, Destinations, Outbox, ShikiruClient, SignalNumbers

NOTHING_TRIED: Final = "nothing, the pull request of the signal is not on record"
SENT: Final = "sent the review message to the PO"
NOTICE_KEY: Final = "review"


class ReviewHandler:
    """Queues the review message of a signal with its number, so the reminders apply to it.

    The title comes from Shikiru's task; when Shikiru cannot give it, the task id stands in. The
    notice is keyed by the signal, so a handler that runs again for a restarted signal queues
    nothing new.
    """

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        numbers: SignalNumbers,
        outbox: Outbox,
        clock: Clock,
        *,
        product: str,
        destinations: Destinations,
    ) -> None:
        self._product = product
        self._clients = clients
        self._numbers = numbers
        self._outbox = outbox
        self._destinations = destinations
        self._clock = clock

    async def handle(self, signal: Signal) -> Outcome:
        pull = signal.pull_request
        if pull is None:
            return Forward(NOTHING_TRIED)
        number = self._numbers.number_of(signal)
        if number is None:
            message = f"{signal.id} is not in the journal"
            raise LookupError(message)
        title = await self._title(signal)
        destinations = self._destinations.for_signal(signal.project, signal.kind)
        in_topic = all(
            self._destinations.component_of(destination.thread) == signal.project
            for destination in destinations
        )
        self._outbox.queue(
            number,
            Purpose.NOTICE,
            destinations,
            review_message(number, signal, pull, title, product=self._product, in_topic=in_topic),
            key=f"{NOTICE_KEY}:{number}",
            now=self._clock.now(),
            standing=True,
        )
        return Notified(SENT)

    async def _title(self, signal: Signal) -> str:
        if signal.task_id is None:
            return ""
        try:
            detail = await self._clients[signal.project].get_task(signal.task_id)
        except ShikiruError:
            return signal.task_id
        return signal.task_id if detail is None else detail.task.title
