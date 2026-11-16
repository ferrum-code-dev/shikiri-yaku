"""The repairs an upgraded journal gets once, when this code first opens it."""

from __future__ import annotations

import re
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
# keep: the name the key rename is recorded under, so a journal is renamed once
RENAMED_KEYS: Final = "renamed-keys"
_BODY = TypeAdapter(Message)
# keep: the commands' old names, as the notice and reply keys they wrote before the rename
_PREFIX_RENAMES: Final = (("ask:", "expert:"), ("goal:", "submit:"), ("open:", "pending:"))
_OPEN_INSTALL: Final = re.compile(r"install:(?P<op>\d+):open:(?P<update>\d+)")


class Repairs:
    """Repairs an upgraded journal once: its older notices and replies, under the keys they had."""

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

    def rename_keys(self) -> int:
        """Rename the notice and reply keys of the old command names, and return how many.

        A tap, a settle or a replay looks up a key, so a copy or reply sent before the rename needs
        its new key to keep its buttons, its final edit and its duplicate guard. The rename and its
        record are one transaction, and a later call renames nothing.
        """
        with self._database.transaction() as connection:
            done = connection.execute(
                "SELECT 1 FROM repairs WHERE name = ?", (RENAMED_KEYS,)
            ).fetchone()
            if done is not None:
                return 0
            notices = [
                (row["id"], row["notice_key"])
                for row in connection.execute(
                    "SELECT id, notice_key FROM messages WHERE notice_key IS NOT NULL"
                )
            ]
            replies = [
                row["reply_key"]
                for row in connection.execute("SELECT reply_key FROM command_replies")
            ]
            notice_moves = [
                (_renamed_key(key), row_id) for row_id, key in notices if _renamed_key(key) != key
            ]
            reply_moves = [(_renamed_key(key), key) for key in replies if _renamed_key(key) != key]
            connection.executemany("UPDATE messages SET notice_key = ? WHERE id = ?", notice_moves)
            connection.executemany(
                "UPDATE command_replies SET reply_key = ? WHERE reply_key = ?", reply_moves
            )
            connection.execute("INSERT INTO repairs (name) VALUES (?)", (RENAMED_KEYS,))
        return len(notice_moves) + len(reply_moves)


def _renamed_key(key: str) -> str:
    for old, new in _PREFIX_RENAMES:
        if key.startswith(old):
            return new + key.removeprefix(old)
    match = _OPEN_INSTALL.fullmatch(key)
    return key if match is None else f"install:{match['op']}:pending:{match['update']}"


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
