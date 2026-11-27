"""The Notifier: an informational message about a signal, queued once per key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, Outbox, Places


class Notifier:
    """Queues a notice about a signal for the signal's places, and only once per key.

    A notice is not a question the PO answers, so it has no signal number in the outbox. The key
    names what the notice is about, so a handler that runs again does not send it twice.
    """

    def __init__(self, outbox: Outbox, places: Places, clock: Clock) -> None:
        self._outbox = outbox
        self._places = places
        self._clock = clock

    def tell(self, signal: Signal, text: str, key: str) -> None:
        self._outbox.queue(
            None,
            Purpose.NOTICE,
            self._places.for_signal(signal.project, signal.kind),
            Message(text),
            key=key,
            now=self._clock.now(),
        )
