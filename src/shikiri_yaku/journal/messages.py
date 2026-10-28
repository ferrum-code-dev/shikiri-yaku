from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal.database import Inserted, stamp
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from shikiri_yaku.journal.database import Database


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


class Delivery(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    RECEIVED = "received"


_BODY = TypeAdapter(Message)


@dataclass(frozen=True)
class StoredMessage:
    id: int
    signal_number: int | None
    channel: str
    chat: str
    platform_message_id: str | None
    purpose: Purpose | None
    delivery: Delivery
    body: Message | None


def _message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        signal_number=row["signal_number"],
        channel=row["channel"],
        chat=row["chat"],
        platform_message_id=row["platform_message_id"],
        purpose=None if row["purpose"] is None else Purpose(row["purpose"]),
        delivery=Delivery(row["delivery"]),
        body=None if row["body"] is None else _BODY.validate_json(row["body"]),
    )


class MessageStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record_outbound(
        self,
        *,
        signal_number: int,
        channel: str,
        chat: str,
        purpose: Purpose,
        body: Message,
        notice_key: str | None,
        now: datetime,
    ) -> Inserted:
        """Journal a message and its body before it is sent; a key queued for that chat is known."""
        with self._database.transaction() as connection:
            created = connection.execute(
                """
                INSERT INTO messages (signal_number, direction, channel, chat, purpose, notice_key,
                                      delivery, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (notice_key, channel, chat) DO NOTHING
                """,
                (
                    signal_number,
                    Direction.OUT,
                    channel,
                    chat,
                    purpose,
                    notice_key,
                    Delivery.PENDING,
                    _BODY.dump_json(body).decode(),
                    stamp(now),
                ),
            )
            if created.rowcount == 1:
                return Inserted(created.lastrowid or 0, is_new=True)
            row = connection.execute(
                "SELECT id FROM messages WHERE notice_key = ? AND channel = ? AND chat = ?",
                (notice_key, channel, chat),
            ).fetchone()
            return Inserted(row["id"], is_new=False)

    def confirm_delivery(self, message_id: int, platform_message_id: str) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ?, platform_message_id = ? WHERE id = ?",
            (Delivery.SENT, platform_message_id, message_id),
        )

    def unconfirmed(self) -> list[StoredMessage]:
        """Return what was recorded and not confirmed; a row without a body cannot be sent."""
        rows = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE direction = ? AND delivery = ? AND body IS NOT NULL
            ORDER BY id
            """,
            (Direction.OUT, Delivery.PENDING),
        )
        return [_message(row) for row in rows]

    def for_signal(self, signal_number: int) -> list[StoredMessage]:
        rows = self._database.connection.execute(
            "SELECT * FROM messages WHERE signal_number = ? AND direction = ? ORDER BY id",
            (signal_number, Direction.OUT),
        )
        return [_message(row) for row in rows]

    def record_inbound(
        self, *, channel: str, chat: str, platform_message_id: str, now: datetime
    ) -> bool:
        """Remember an inbound item; False means it was seen before."""
        created = self._database.connection.execute(
            """
            INSERT INTO messages (direction, channel, chat, platform_message_id, delivery,
                                  created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (Direction.IN, channel, chat, platform_message_id, Delivery.RECEIVED, stamp(now)),
        )
        return created.rowcount == 1
