"""The one sender: delivers every recorded outbound message (design §7, §10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from shikiri_yaku.ports import Channel, Clock, Outbox


class OutboxSender:
    """Sends what the outbox holds and confirms each message after the Channel accepted it.

    A failed send stays pending and is tried again on the next tick. A crash between the Channel
    accepting a message and the confirmation sends it once more; its body carries `S<number>`, so
    the PO can recognize the duplicate.
    """

    def __init__(
        self, outbox: Outbox, channel: Channel, clock: Clock, *, interval_s: float
    ) -> None:
        self._outbox = outbox
        self._channel = channel
        self._clock = clock
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        for pending in self._outbox.pending():
            try:
                ref = await self._channel.send(pending.destination.chat, pending.message)
            except ChannelError:
                continue
            self._outbox.confirm(pending.message_id, ref)
