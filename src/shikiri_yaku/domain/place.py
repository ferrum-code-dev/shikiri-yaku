"""Where a message is, as the core knows it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Place:
    """A place of a channel; only that channel reads `id`."""

    channel: str
    id: str
