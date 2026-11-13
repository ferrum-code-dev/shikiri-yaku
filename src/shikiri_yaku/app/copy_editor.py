"""Edits every copy of a resolved question to its final state, without buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.ports import Channel, Clock, FinalEdits


class CopyEditor:
    """Records the final state of each copy, then asks the platform to show it.

    The final state is journaled before any edit, so a restart finishes what was started. A failure
    the platform may repeat leaves the edit recorded for the next tick; one it refuses for good is
    given up, and the PO is notified of it by the Channel. The journal supplies the line, whether
    the PO's own resolve was confirmed or the PO resolved elsewhere; this class only applies it.
    """

    def __init__(
        self, log: FinalEdits, channel: Channel, clock: Clock, *, interval_s: float
    ) -> None:
        self._log = log
        self._channel = channel
        self._clock = clock
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        for number, line in self._log.unsettled():
            self._log.settle(number, line)
        for edit in self._log.to_edit():
            try:
                await self._channel.edit(edit.message_ref, edit.message)
            except ChannelRefusedError as error:
                self._log.give_up(edit.message_ref, edit.message, str(error))
            except ChannelError:
                continue
            else:
                self._log.confirm_edit(edit.message_ref, edit.message)
