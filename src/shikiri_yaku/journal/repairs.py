"""The repairs an upgraded journal gets once, when this code first opens it (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal.messages import Direction
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from shikiri_yaku.domain.extension import LegacyCopy
    from shikiri_yaku.journal.database import Database

# keep: the name the standing repair is recorded under, so a journal is repaired once
LEGACY_STANDING: Final = "legacy-standing"
_BODY = TypeAdapter(Message)


class Repairs:
    """Marks the notices sent before the standing flag existed, once per journal."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def adopt_legacy_standing(self, copies: Sequence[LegacyCopy]) -> int:
        """Mark the copies their categories recognize as standing, and return how many.

        A copy is recognized when its notice key is the category's prefix and its signal's number,
        its signal has one of the category's kinds, and it carries the Stop reminders button of
        that number. The marks and the record of the repair are one transaction. A later call marks
        nothing, so a copy queued after the upgrade stands only when its handler says so.
        """
        with self._database.transaction() as connection:
            done = connection.execute(
                "SELECT 1 FROM repairs WHERE name = ?", (LEGACY_STANDING,)
            ).fetchone()
            if done is not None:
                return 0
            found = {row_id for copy in copies for row_id in _recognized(connection, copy)}
            connection.executemany(
                "UPDATE messages SET standing = 1 WHERE id = ?", [(row_id,) for row_id in found]
            )
            connection.execute("INSERT INTO repairs (name) VALUES (?)", (LEGACY_STANDING,))
        return len(found)


def _recognized(connection: sqlite3.Connection, copy: LegacyCopy) -> list[int]:
    rows = connection.execute(
        """
        SELECT m.id, m.signal_number, m.body, s.kind
        FROM messages m JOIN signals s ON s.number = m.signal_number
        WHERE m.direction = :out AND m.purpose = :notice AND m.standing = 0
            AND m.body IS NOT NULL AND m.notice_key = :prefix || m.signal_number
            AND (:requires_pull_request = 0 OR
                (s.pr_url IS NOT NULL AND s.pr_commits IS NOT NULL))
        """,
        {
            "out": Direction.OUT,
            "notice": Purpose.NOTICE,
            "prefix": copy.key_prefix,
            "requires_pull_request": copy.requires_pull_request,
        },
    ).fetchall()
    return [
        row["id"]
        for row in rows
        if row["kind"] in copy.kinds and _has_stop_button(row["body"], row["signal_number"])
    ]


def _has_stop_button(body: str, number: int) -> bool:
    data = stop_reminders_data(number)
    return any(button.data == data for button in _BODY.validate_json(body).buttons)
