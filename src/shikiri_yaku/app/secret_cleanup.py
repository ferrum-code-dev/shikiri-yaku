"""Deletes the chat messages that carried a secret, after the attempt is journaled (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.ports import Channel, Clock, SecretLog


class SecretCleanup:
    """Asks the platform to delete each refused message once; the attempt is journaled first.

    An attempt that fails, refused or not, is not repeated: the journal keeps it as attempted. The
    refusal of the item already stands, and the item's text was never kept.
    """

    def __init__(
        self, log: SecretLog, channel: Channel, clock: Clock, *, interval_s: float
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
        for item in self._log.to_delete():
            self._log.record_deletion(item.update_id)
            try:
                await self._channel.delete(item.message_ref)
            except ChannelError, ChannelRefusedError:
                continue
            self._log.confirm_deletion(item.update_id)
