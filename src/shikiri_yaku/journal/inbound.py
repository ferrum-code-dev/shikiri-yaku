from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.messages import Delivery, Direction

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database


def _inbound(row: sqlite3.Row) -> Inbound:
    return Inbound(
        update_id=row["update_id"],
        kind=InboundKind(row["kind"]),
        sender=row["sender"],
        chat=int(row["chat"]),
        message_ref=row["message_ref"],
        reply_to_ref=row["reply_to_ref"],
        text=row["body"],
        data=row["data"],
        thread=row["thread"],
    )


class JournalInbound:
    """What the PO sent: inbound rows of the messages table, one per update id.

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
                self._insert(connection, channel, item, now)
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
            "SELECT * FROM messages WHERE direction = ? AND delivery = ? ORDER BY update_id",
            (Direction.IN, Delivery.RECEIVED),
        )
        return [_inbound(row) for row in rows]

    def mark_handled(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.HANDLED, update_id, Delivery.RECEIVED),
        )

    def to_delete(self) -> list[Inbound]:
        # keep: a tap's message is the bot's own question, so only what the PO typed is deleted
        rows = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE direction = ? AND delivery = ? AND kind IN (?, ?)
            ORDER BY update_id
            """,
            (Direction.IN, Delivery.REFUSED, InboundKind.TEXT, InboundKind.REPLY),
        )
        return [_inbound(row) for row in rows]

    def record_deletion(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.DELETE_ATTEMPTED, update_id, Delivery.REFUSED),
        )

    def defer_deletion(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.REFUSED, update_id, Delivery.DELETE_ATTEMPTED),
        )

    def confirm_deletion(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.DELETED, update_id, Delivery.DELETE_ATTEMPTED),
        )

    @staticmethod
    def _insert(connection: sqlite3.Connection, channel: str, item: Inbound, now: datetime) -> None:
        """Insert an item once; a secret keeps its form and loses its text and data."""
        form = secret_form([text for text in (item.text, item.data) if text is not None])
        connection.execute(
            """
            INSERT INTO messages (direction, channel, chat, update_id, kind, sender, message_ref,
                                  reply_to_ref, body, data, delivery, refusal, created_at, thread)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (update_id) WHERE update_id IS NOT NULL DO NOTHING
            """,
            (
                Direction.IN,
                channel,
                str(item.chat),
                item.update_id,
                item.kind,
                item.sender,
                item.message_ref,
                item.reply_to_ref,
                None if form else item.text,
                None if form else item.data,
                Delivery.RECEIVED if form is None else Delivery.REFUSED,
                form,
                stamp(now),
                item.thread,
            ),
        )
