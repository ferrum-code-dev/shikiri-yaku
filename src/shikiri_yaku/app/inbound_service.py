"""The inbound job: polls the PO's chat and hands stored items to Replies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import ChannelError, ChannelProtocolError, ShikiruError

if TYPE_CHECKING:
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.inbound_poller import InboundPoller
    from shikiri_yaku.app.replies import Replies
    from shikiri_yaku.ports import Clock, InboundLog

# keep: the batch is the what-failed key of the Operator's own health signal
INBOUND_BATCH: Final = "inbound_batch"


class InboundService:
    """Polls, then applies every pending item; a restart finds the pending items still stored.

    A transport failure is tried again on the next tick. A batch that cannot be read opens one
    `health` signal, which the next readable batch closes. An item whose Shikiru call fails stays
    pending, and the items after it wait, so answers are applied in the order they were sent.
    """

    def __init__(
        self,
        poller: InboundPoller,
        log: InboundLog,
        replies: Replies,
        health: HealthSource,
        clock: Clock,
        *,
        interval_s: float,
        health_scope: str = INBOUND_BATCH,
    ) -> None:
        self._poller = poller
        self._log = log
        self._replies = replies
        self._health = health
        self._clock = clock
        self._interval_s = interval_s
        self._health_scope = health_scope

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        await self._poll()
        for item in self._log.pending():
            try:
                await self._replies.handle(item)
            except ShikiruError:
                return
            self._log.mark_handled(item.update_id)

    async def _poll(self) -> None:
        try:
            await self._poller.poll_once()
        except ChannelProtocolError as error:
            self._health.source_failed(self._health_scope, error)
        except ChannelError:
            pass
        else:
            self._health.source_recovered(self._health_scope)
