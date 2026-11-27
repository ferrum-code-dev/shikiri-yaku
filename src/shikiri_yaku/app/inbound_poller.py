"""Brings what the PO sent into the journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from shikiri_yaku.domain.inbound import Received
    from shikiri_yaku.ports import Clock, InboundLog, Inbox


class InboundPoller:
    """Poll once, retaining owners' events and advancing the cursor atomically.

    An event is kept when an owner sent it. Anything else is dropped, and the cursor still moves
    past it. The Inbox decides which chats an event may come from.
    """

    def __init__(
        self,
        inbox: Inbox,
        log: InboundLog,
        clock: Clock,
        *,
        channel: str,
        owners: Collection[str],
        wait_s: float,
    ) -> None:
        self._inbox = inbox
        self._log = log
        self._clock = clock
        self._channel = channel
        self._owners = frozenset(owners)
        self._wait_s = wait_s

    async def poll_once(self) -> None:
        batch = await self._inbox.fetch(self._log.cursor(self._channel), self._wait_s)
        if batch.cursor is None:
            return
        events = [event for event in batch.events if self._served(event)]
        self._log.record_batch(self._channel, events, batch.cursor, self._clock.now())

    def _served(self, event: Received) -> bool:
        return event.sender in self._owners
