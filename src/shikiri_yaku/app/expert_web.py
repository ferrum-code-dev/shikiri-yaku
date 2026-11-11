"""Present web requests and apply the PO's decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from shikiri_yaku.domain.callback import WEB_ALLOW_VERB, WEB_DENY_VERB, decode_callback
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import Button, Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox


class WebRequests(Protocol):
    """The durable web requests and their decisions."""

    def pending(self, update_id: int | None = None) -> list[tuple[int, int, int, str]]: ...
    def bound(self, channel: str, chat: str, message_ref: str) -> str | None: ...
    def decide(
        self, request_id: int, update_id: int, *, allow: bool, now: datetime
    ) -> int | None: ...


class ExpertWebRequests:
    """Show each requested host once, and continue the conversation on a decision."""

    def __init__(
        self, log: WebRequests, outbox: Outbox, clock: Clock, *, channel: str, group_id: int
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._channel = channel
        self._group_id = group_id

    def queue(self, update_id: int | None = None) -> None:
        for request_id, conversation_id, thread_id, host in self._log.pending(update_id):
            self._outbox.queue(
                None,
                Purpose.EXPERT,
                [Destination(self._channel, str(self._group_id), thread_id)],
                Message(
                    f"Allow web access to {host}?",
                    (
                        Button("Allow", f"{WEB_ALLOW_VERB}:{request_id}"),
                        Button("Deny", f"{WEB_DENY_VERB}:{request_id}"),
                    ),
                ),
                key=f"expert:web:{request_id}",
                now=self._clock.now(),
                conversation_id=conversation_id,
            )

    async def tap(self, item: Inbound, run: Callable[[int, int], Awaitable[None]]) -> bool:
        try:
            tap = decode_callback(item.data or "")
        except ValidationError:
            return False
        if tap[0] not in (WEB_ALLOW_VERB, WEB_DENY_VERB):
            return False
        if (
            item.chat != self._group_id
            or self._log.bound(self._channel, str(item.chat), item.message_ref)
            != f"expert:web:{tap[1]}"
        ):
            return True
        conversation_id = self._log.decide(
            tap[1], item.update_id, allow=tap[0] == WEB_ALLOW_VERB, now=self._clock.now()
        )
        if conversation_id is not None:
            await run(conversation_id, item.update_id)
        return True
