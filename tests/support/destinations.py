"""A Destinations fake: every signal goes to the same destinations; no thread names a component."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.domain.signal import Kind


class FixedDestinations:
    def __init__(self, destinations: Sequence[Destination] = ()) -> None:
        self._destinations = list(destinations)

    def for_signal(self, project: str, kind: Kind) -> list[Destination]:  # noqa: ARG002 -- the port's shape
        return list(self._destinations)

    def component_of(self, thread: int | None) -> str | None:  # noqa: ARG002 -- the port's shape
        return None

    async def prepare(self) -> None:
        """Nothing to reconcile in a fake."""
