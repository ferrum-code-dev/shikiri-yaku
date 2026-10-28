"""Brings what the PO sent into the journal (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from shikiri_yaku.ports import Clock, InboundLog, Updates


class InboundPoller:
    """Poll once, retaining owners' items and advancing the offset atomically."""

    def __init__(
        self,
        updates: Updates,
        log: InboundLog,
        clock: Clock,
        *,
        channel: str,
        owners: Collection[int],
        wait_s: float,
    ) -> None:
        self._updates = updates
        self._log = log
        self._clock = clock
        self._channel = channel
        self._owners = frozenset(owners)
        self._wait_s = wait_s

    async def poll_once(self) -> None:
        batch = await self._updates.fetch(self._log.next_offset(self._channel), self._wait_s)
        if batch.last_update_id is None:
            return
        items = [item for item in batch.items if item.sender in self._owners]
        self._log.record_batch(self._channel, items, batch.last_update_id + 1, self._clock.now())
