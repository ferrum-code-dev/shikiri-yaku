from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.journal.database import stamp

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database


class InboundState(StrEnum):
    PENDING = "pending"
    HANDLED = "handled"
    REFUSED = "refused"


def _inbound(row: sqlite3.Row) -> Inbound:
    return Inbound(
        update_id=row["update_id"],
        kind=InboundKind(row["kind"]),
        sender=row["sender"],
        message_ref=row["message_ref"],
        reply_to_ref=row["reply_to_ref"],
        text=row["text"],
        data=row["data"],
    )


class JournalInbound:
    """What the PO sent, stored before the polling offset moves past it (design §7).

    Update ids are unique per bot, and an instance has one bot, so the id alone identifies an item.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def next_offset(self, channel: str) -> int:
        row = self._database.connection.execute(
            "SELECT next_offset FROM polling WHERE channel = ?", (channel,)
        ).fetchone()
        return 0 if row is None else row["next_offset"]

    def record_batch(
        self, channel: str, items: Sequence[Inbound], next_offset: int, now: datetime
    ) -> None:
        # keep: one transaction, so a crash leaves the offset behind and the batch is fetched again
        with self._database.transaction() as connection:
            for item in items:
                self._insert(connection, item, now)
            connection.execute(
                """
                INSERT INTO polling (channel, next_offset) VALUES (?, ?)
                ON CONFLICT (channel) DO UPDATE SET
                    next_offset = MAX(next_offset, excluded.next_offset)
                """,
                (channel, next_offset),
            )

    def pending(self) -> list[Inbound]:
        rows = self._database.connection.execute(
            "SELECT * FROM inbound WHERE state = ? ORDER BY update_id", (InboundState.PENDING,)
        )
        return [_inbound(row) for row in rows]

    def mark_handled(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE inbound SET state = ? WHERE update_id = ? AND state = ?",
            (InboundState.HANDLED, update_id, InboundState.PENDING),
        )

    @staticmethod
    def _insert(connection: sqlite3.Connection, item: Inbound, now: datetime) -> None:
        """Insert an item once; a secret keeps its form and loses its text and data."""
        texts = [text for text in (item.text, item.data) if text is not None]
        form = secret_form(texts)
        connection.execute(
            """
            INSERT INTO inbound (update_id, kind, sender, message_ref, reply_to_ref, text, data,
                                 state, refusal, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (update_id) DO NOTHING
            """,
            (
                item.update_id,
                item.kind,
                item.sender,
                item.message_ref,
                item.reply_to_ref,
                None if form else item.text,
                None if form else item.data,
                InboundState.PENDING if form is None else InboundState.REFUSED,
                form,
                stamp(now),
            ),
        )
