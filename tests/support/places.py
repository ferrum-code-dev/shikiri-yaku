"""A Places fake: every signal goes to the same places.

`topics` maps a thread to the component its topic names; a thread not listed names none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.adapters.telegram import chat_and_thread

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.domain.place import Place
    from shikiri_yaku.domain.signal import Kind


class FixedPlaces:
    def __init__(
        self,
        places: Sequence[Place] = (),
        *,
        topics: Mapping[int, str] | None = None,
    ) -> None:
        self._places = list(places)
        self._topics = dict(topics or {})

    def for_signal(self, project: str, kind: Kind) -> list[Place]:  # noqa: ARG002 -- the port's shape
        return list(self._places)

    def component_of(self, place: Place) -> str | None:
        _, thread = chat_and_thread(place)
        return None if thread is None else self._topics.get(thread)

    async def prepare(self) -> None:
        """Nothing to reconcile in a fake."""
