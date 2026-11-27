"""Present web requests and apply the PO's decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from shikiri_yaku.domain.callback import WEB_ALLOW_VERB, WEB_DENY_VERB, decode_callback
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.ports import Button, Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from shikiri_yaku.app.expert_admin import AdminAccess
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox, Rooms


class WebRequests(Protocol):
    """The durable web requests and their decisions."""

    def pending(self, event_id: int | None = None) -> list[tuple[int, int, str, str]]: ...
    def conversation_of(self, request_id: int) -> int | None: ...
    def bound(self, channel: str, message_ref: str) -> str | None: ...
    def decide(
        self, request_id: int, event_id: int, *, allow: bool, now: datetime
    ) -> int | None: ...
    def settle_decided(self) -> None: ...


class ExpertWebRequests:
    """Show each requested host once, and continue the conversation on a decision."""

    def __init__(
        self,
        log: WebRequests,
        outbox: Outbox,
        clock: Clock,
        *,
        rooms: Rooms,
        access: AdminAccess,
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._rooms = rooms
        self._access = access

    def queue(self, event_id: int | None = None) -> None:
        for request_id, conversation_id, room, host in self._log.pending(event_id):
            self._outbox.queue(
                None,
                Purpose.EXPERT,
                [self._rooms.place(room)],
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

    def recover(self) -> None:
        self._log.settle_decided()

    async def tap(self, item: Inbound, run: Callable[[int, int], Awaitable[None]]) -> bool:
        try:
            tap = decode_callback(item.data or "")
        except ValidationError:
            return False
        if tap[0] not in (WEB_ALLOW_VERB, WEB_DENY_VERB):
            return False
        bound = self._log.bound(item.place.channel, item.message_ref)
        if bound != f"expert:web:{tap[1]}":
            return True
        owner = self._log.conversation_of(tap[1])
        if owner is None or not self._access.admit(owner, item.sender):
            return True
        conversation_id = self._log.decide(
            tap[1], item.id, allow=tap[0] == WEB_ALLOW_VERB, now=self._clock.now()
        )
        if conversation_id is not None:
            await run(conversation_id, item.id)
        return True
