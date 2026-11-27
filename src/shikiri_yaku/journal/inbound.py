from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import FileRef, Inbound, InboundKind, Received
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.messages import Delivery, Direction

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database


def _inbound(row: sqlite3.Row) -> Inbound:
    kind = InboundKind(row["kind"])
    is_file = kind == InboundKind.FILE and row["data"] is not None
    return Inbound(
        id=row["update_id"],
        key=row["event_key"],
        kind=kind,
        sender=row["sender"],
        chat=int(row["chat"]),
        message_ref=row["message_ref"],
        reply_to_ref=row["reply_to_ref"],
        text=row["body"],
        data=None if is_file else row["data"],
        thread=row["thread"],
        file=FileRef.model_validate_json(row["data"]) if is_file else None,
    )


class JournalInbound:
    """What the PO sent: inbound rows of the messages table, one per channel and event key.

    The journal numbers the events of a channel from a counter it never moves back, and keeps the
    number in the update_id column. The number is unique across channels and identifies an event
    everywhere else. The keys stay when an event's row is erased.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def cursor(self, channel: str) -> str | None:
        row = self._database.connection.execute(
            "SELECT cursor FROM polling WHERE channel = ?", (channel,)
        ).fetchone()
        return None if row is None else row["cursor"]

    def record_batch(
        self, channel: str, events: Sequence[Received], cursor: str, now: datetime
    ) -> None:
        # keep: one transaction, so a crash leaves the cursor behind and the batch is fetched again
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO polling (channel, cursor) VALUES (?, ?)
                ON CONFLICT (channel) DO UPDATE SET cursor = excluded.cursor
                """,
                (channel, cursor),
            )
            for event in events:
                self._insert(connection, channel, event, now)

    def pending(self) -> list[Inbound]:
        rows = self._database.connection.execute(
            "SELECT * FROM messages WHERE direction = ? AND delivery = ? ORDER BY update_id",
            (Direction.IN, Delivery.RECEIVED),
        )
        return [_inbound(row) for row in rows]

    def mark_handled(self, event_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.HANDLED, event_id, Delivery.RECEIVED),
        )

    def to_delete(self) -> list[Inbound]:
        # keep: a tap's message is the bot's own question, so only what the PO typed is deleted
        rows = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE direction = ? AND delivery = ? AND kind IN (?, ?, ?)
            ORDER BY update_id
            """,
            (
                Direction.IN,
                Delivery.REFUSED,
                InboundKind.TEXT,
                InboundKind.REPLY,
                InboundKind.FILE,
            ),
        )
        return [_inbound(row) for row in rows]

    def record_deletion(self, event_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.DELETE_ATTEMPTED, event_id, Delivery.REFUSED),
        )

    def defer_deletion(self, event_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.REFUSED, event_id, Delivery.DELETE_ATTEMPTED),
        )

    def confirm_deletion(self, event_id: int) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE update_id = ? AND delivery = ?",
            (Delivery.DELETED, event_id, Delivery.DELETE_ATTEMPTED),
        )

    @staticmethod
    def _insert(
        connection: sqlite3.Connection, channel: str, event: Received, now: datetime
    ) -> None:
        """Insert an event once; a secret keeps its form and loses its text and data."""
        new = connection.execute(
            "INSERT INTO event_keys (channel, event_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (channel, event.key),
        ).rowcount
        if not new:
            return
        data = event.data if event.file is None else event.file.model_dump_json()
        form = secret_form([text for text in (event.text, data) if text is not None])
        # keep: rows of all channels share the id columns, so each channel counts past them all
        number = connection.execute(
            "UPDATE polling SET last_id = 1 + (SELECT MAX(last_id) FROM polling)"
            " WHERE channel = ? RETURNING last_id",
            (channel,),
        ).fetchone()["last_id"]
        connection.execute(
            """
            INSERT INTO messages (direction, channel, chat, update_id, event_key, kind, sender,
                                  message_ref, reply_to_ref, body, data, delivery, refusal,
                                  created_at, thread)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                Direction.IN,
                channel,
                str(event.chat),
                number,
                event.key,
                event.kind,
                event.sender,
                event.message_ref,
                event.reply_to_ref,
                None if form else event.text,
                None if form else data,
                Delivery.RECEIVED if form is None else Delivery.REFUSED,
                form,
                stamp(now),
                event.thread,
            ),
        )
