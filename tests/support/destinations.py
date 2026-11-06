"""A Destinations fake: every signal goes to the same destinations.

`topics` maps a thread to the component its topic names; a thread not listed names none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.domain.signal import Kind


class FixedDestinations:
    def __init__(
        self,
        destinations: Sequence[Destination] = (),
        *,
        topics: Mapping[int, str] | None = None,
    ) -> None:
        self._destinations = list(destinations)
        self._topics = dict(topics or {})

    def for_signal(self, project: str, kind: Kind) -> list[Destination]:  # noqa: ARG002 -- the port's shape
        return list(self._destinations)

    def component_of(self, thread: int | None) -> str | None:
        return None if thread is None else self._topics.get(thread)

    async def prepare(self) -> None:
        """Nothing to reconcile in a fake."""
