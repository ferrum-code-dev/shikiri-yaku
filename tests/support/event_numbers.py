"""Line the journal's event numbers up with the ids a test names."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shikiri_yaku.journal import Database


def number_next_event(database: Database, channel: str, number: int) -> None:
    """Make the journal give the channel's next event `number`, as after that many events."""
    database.connection.execute(
        "INSERT INTO polling (channel, last_id) VALUES (?, ?)"
        " ON CONFLICT (channel) DO UPDATE SET last_id = excluded.last_id",
        (channel, number - 1),
    )
