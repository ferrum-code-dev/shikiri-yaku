"""The one sender: delivers every recorded outbound message."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from shikiri_yaku.ports import TOPIC_GONE_REASON, ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import Channel, Clock, Outbox


class TopicGone(Protocol):
    """Ends a conversation whose topic the Channel no longer has."""

    async def topic_gone(self, conversation_id: int, destination: Destination) -> None: ...


class OutboxSender:
    """Sends what the outbox holds and confirms each message after the Channel accepted it.

    A failed send stays pending and is tried again on the next tick. A message the Channel refuses
    for good is recorded as refused and never offered again; when the refusal says the topic is
    gone, its conversation ends too. A crash between the Channel accepting a message and the
    confirmation sends it once more; its body carries `S<number>`, so the PO can recognize the
    duplicate.
    """

    def __init__(
        self,
        outbox: Outbox,
        channel: Channel,
        clock: Clock,
        *,
        interval_s: float,
        gone: TopicGone | None = None,
    ) -> None:
        self._outbox = outbox
        self._channel = channel
        self._clock = clock
        self._interval_s = interval_s
        self._gone = gone

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        conversations_with_failed_sends: set[int] = set()
        for candidate in self._outbox.pending():
            if candidate.conversation_id in conversations_with_failed_sends:
                continue
            pending = self._outbox.pending_message(candidate.message_id)
            if pending is None:
                continue
            try:
                ref = await self._channel.send(pending.destination, pending.message)
            except ChannelRefusedError as error:
                self._outbox.refuse(pending.message_id, str(error), now=self._clock.now())
                if (
                    self._gone is not None
                    and pending.conversation_id is not None
                    and str(error) == TOPIC_GONE_REASON
                ):
                    await self._gone.topic_gone(pending.conversation_id, pending.destination)
                continue
            except ChannelError:
                if pending.conversation_id is not None:
                    conversations_with_failed_sends.add(pending.conversation_id)
                continue
            self._outbox.confirm(pending.message_id, ref)
