"""Deletes the copies a newer copy of the same slot has replaced, after the attempt is journaled."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import REPLACED_LINE
from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.ports import Channel, Clock, Replacements


class CopyReplacer:
    """Asks the platform to delete each replaced copy; the attempt is journaled first.

    A deletion the platform refuses for good (ChannelRefusedError, e.g. a copy older than the
    platform allows) is not repeated: the copy keeps its text and is edited to say it was replaced.
    A transient failure (ChannelError) puts the copy back, and the next tick tries again. An attempt
    cut short by a crash stays attempted and is not repeated, because its outcome is unknown.
    """

    def __init__(
        self, log: Replacements, channel: Channel, clock: Clock, *, interval_s: float
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
        for message_ref in self._log.superseded():
            self._log.begin_deletion(message_ref)
            try:
                await self._channel.delete(message_ref)
            except ChannelRefusedError as error:
                self._log.keep_replaced(message_ref, REPLACED_LINE, str(error))
            except ChannelError:
                self._log.undo_deletion(message_ref)
            else:
                self._log.confirm_deletion(message_ref)
