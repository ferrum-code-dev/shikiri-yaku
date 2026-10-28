"""Answers the PO's plain text: `/status` gets the status, anything else the list (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox

_STATUS: Final = "/status"
_OPEN: Final = "/open"
_HELP: Final = (
    f"{_STATUS} · each component's engine, tasks and quota blocks\n"
    f"{_OPEN} · open escalations and pending approvals, shown again\n"
    "/help · this list"
)
_KEY_PREFIX: Final = "command:"


class Commands:
    """Answers `/open` and `/status`; any other text gets the command list.

    A command reply has no signal, so it is journaled with none. Its notice key is the update id:
    a repeated delivery of the same update queues nothing.
    """

    def __init__(
        self,
        outbox: Outbox,
        clock: Clock,
        status: Status,
        open_command: OpenCommand,
        *,
        channel: str,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._status = status
        self._open = open_command
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        command = (item.text or "").strip()
        if command == _OPEN:
            await self._open.handle(item)
            return
        text = await self._status.report() if command == _STATUS else _HELP
        self._outbox.queue(
            None,
            Purpose.NOTICE,
            [Destination(self._channel, str(item.sender))],
            Message(text),
            key=f"{_KEY_PREFIX}{item.update_id}",
            now=self._clock.now(),
        )
