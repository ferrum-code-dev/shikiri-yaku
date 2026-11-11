"""The inbound job: polls the PO's chat and hands stored items to Replies or the Expert."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.ports import ChannelError, ChannelProtocolError, ShikiruError

if TYPE_CHECKING:
    from shikiri_yaku.app.expert_turns import ExpertTurns
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
    pending, and ordinary items after it wait. Independent `/ask` requests and messages in Expert
    topics run concurrently and remain pending until their work finishes; the Expert orders the
    turns of each conversation itself.
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
        expert: ExpertTurns | None = None,
    ) -> None:
        self._poller = poller
        self._log = log
        self._replies = replies
        self._health = health
        self._clock = clock
        self._interval_s = interval_s
        self._health_scope = health_scope
        self._expert = expert
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def run(self) -> None:
        try:
            while True:
                await self.tick()
                await self._clock.sleep(self._interval_s)
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def tick(self) -> None:
        for update_id, task in tuple(self._tasks.items()):
            if task.done():
                del self._tasks[update_id]
                task.result()
        await self._poll()
        for item in self._log.pending():
            if self._background(item):
                if item.update_id not in self._tasks:
                    self._tasks[item.update_id] = asyncio.create_task(self._run_background(item))
                continue
            try:
                await self._replies.handle(item)
            except ShikiruError, ChannelError:
                return
            self._log.mark_handled(item.update_id)

    def _background(self, item: Inbound) -> bool:
        return _is_ask(item) or (self._expert is not None and self._expert.owns(item))

    async def _run_background(self, item: Inbound) -> None:
        try:
            if self._expert is not None and self._expert.owns(item):
                await self._expert.handle(item)
            else:
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
