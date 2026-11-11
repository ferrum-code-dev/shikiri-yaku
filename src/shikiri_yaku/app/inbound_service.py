"""The inbound job: polls the PO's chat and hands stored items to Replies."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.ports import ChannelError, ChannelProtocolError, ShikiruError

if TYPE_CHECKING:
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.inbound_poller import InboundPoller
    from shikiri_yaku.app.replies import Replies
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, InboundLog

# keep: the batch is the what-failed key of the Operator's own health signal
INBOUND_BATCH: Final = "inbound_batch"
_ASK: Final = "/ask"


class InboundService:
    """Polls, then applies every pending item; a restart finds the pending items still stored.

    A transport failure is tried again on the next tick. A batch that cannot be read opens one
    `health` signal, which the next readable batch closes. An item whose Shikiru call fails stays
    pending, and ordinary items after it wait. Independent `/ask` requests run concurrently and
    remain pending until their work finishes.
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
        self._asks: dict[int, asyncio.Task[None]] = {}

    async def run(self) -> None:
        try:
            while True:
                await self.tick()
                await self._clock.sleep(self._interval_s)
        finally:
            for task in self._asks.values():
                task.cancel()
            await asyncio.gather(*self._asks.values(), return_exceptions=True)

    async def tick(self) -> None:
        for update_id, task in tuple(self._asks.items()):
            if task.done():
                del self._asks[update_id]
                task.result()
        await self._poll()
        for item in self._log.pending():
            if _is_ask(item):
                if item.update_id not in self._asks:
                    self._asks[item.update_id] = asyncio.create_task(self._handle_ask(item))
                continue
            try:
                await self._replies.handle(item)
            except ShikiruError, ChannelError:
                return
            self._log.mark_handled(item.update_id)

    async def _handle_ask(self, item: Inbound) -> None:
        try:
            await self._replies.handle(item)
        except ShikiruError, ChannelError:
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


def _is_ask(item: Inbound) -> bool:
    words = (item.text or "").split(maxsplit=1)
    return (
        item.kind == InboundKind.TEXT and bool(words) and words[0].split("@", 1)[0].lower() == _ASK
    )
