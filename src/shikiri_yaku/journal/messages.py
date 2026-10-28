from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.secrets import FORMS, refusal_notice, secret_form
from shikiri_yaku.journal.database import Inserted, stamp
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.ports import Reporter

# keep: the notice key of a refusal's notice is this prefix and the refused row's id
REFUSAL_KEY_PREFIX: Final = "refused:"


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


class Delivery(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    REFUSED = "refused"
    RECEIVED = "received"
    DELETE_ATTEMPTED = "delete_attempted"
    DELETED = "deleted"


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
    refusal: str | None


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
        refusal=row["refusal"],
    )


def _is_notice_of_refusal(notice_key: str | None) -> bool:
    return notice_key is not None and notice_key.startswith(REFUSAL_KEY_PREFIX)


class MessageStore:
    def __init__(self, database: Database, reporter: Reporter) -> None:
        self._database = database
        self._reporter = reporter

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
        """Journal a message and its body before it is sent; a key queued for that chat is known.

        A body that carries a secret is refused here: its form is kept, its text is not, and the PO
        gets the notice of the refusal.
        """
        form = secret_form(body.texts())
        with self._database.transaction():
            inserted = self._insert(
                signal_number=signal_number,
                channel=channel,
                chat=chat,
                purpose=purpose,
                notice_key=notice_key,
                now=now,
                body=None if form is not None else _BODY.dump_json(body).decode(),
                refusal=form,
            )
            if form is not None and inserted.is_new:
                self._notify(
                    signal_number=signal_number,
                    channel=channel,
                    chat=chat,
                    refused_id=inserted.row_id,
                    form=form,
                    now=now,
                )
        return inserted

    def _insert(
        self,
        *,
        signal_number: int,
        channel: str,
        chat: str,
        purpose: Purpose,
        notice_key: str | None,
        now: datetime,
        body: str | None,
        refusal: str | None,
    ) -> Inserted:
        """Insert an outbound row, or find the row that has its notice key for that chat."""
        connection = self._database.connection
        delivery = Delivery.PENDING if refusal is None else Delivery.REFUSED
        created = connection.execute(
            """
            INSERT INTO messages (signal_number, direction, channel, chat, purpose, notice_key,
                                  delivery, body, refusal, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (notice_key, channel, chat) DO NOTHING
            """,
            (
                signal_number,
                Direction.OUT,
                channel,
                chat,
                purpose,
                notice_key,
                delivery,
                body,
                refusal,
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

    def _notify(
        self,
        *,
        signal_number: int,
        channel: str,
        chat: str,
        refused_id: int,
        form: str,
        now: datetime,
    ) -> None:
        """Queue the PO's notice of a refusal; if it cannot be journaled, report its metadata only.

        The notice is a fixed text with the S-number and no refused content, and a refusal of the
        notice itself queues nothing: that case is reported too.
        """
        notice = Message(refusal_notice(signal_number))
        try:
            self._insert(
                signal_number=signal_number,
                channel=channel,
                chat=chat,
                purpose=Purpose.NOTICE,
                notice_key=f"{REFUSAL_KEY_PREFIX}{refused_id}",
                now=now,
                body=_BODY.dump_json(notice).decode(),
                refusal=None,
            )
        except sqlite3.Error:
            self._reporter.report(
                f"S{signal_number} refused ({form}); its notice was not journaled"
            )

    def confirm_delivery(self, message_id: int, platform_message_id: str) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ?, platform_message_id = ? WHERE id = ?",
            (Delivery.SENT, platform_message_id, message_id),
        )

    def refuse_delivery(self, message_id: int, reason: str, now: datetime) -> None:
        """Drop the body of a message the platform refused for good; only the reason stays.

        A refused secret queues the PO a notice. A refused notice is reported instead.
        """
        connection = self._database.connection
        with self._database.transaction():
            row = connection.execute(
                """
                SELECT signal_number, channel, chat, notice_key FROM messages
                WHERE id = ? AND delivery = ?
                """,
                (message_id, Delivery.PENDING),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE messages SET delivery = ?, body = NULL, refusal = ? WHERE id = ?",
                (Delivery.REFUSED, reason, message_id),
            )
            if _is_notice_of_refusal(row["notice_key"]):
                self._reporter.report(f"S{row['signal_number']} notice refused ({reason})")
            elif reason in FORMS:
                self._notify(
                    signal_number=row["signal_number"],
                    channel=row["channel"],
                    chat=row["chat"],
                    refused_id=message_id,
                    form=reason,
                    now=now,
                )

    def record_deletion(self, message_ref: str, reason: str) -> bool:
        """Journal an attempt to delete the copy at this reference, before it is invoked.

        The body of the copy is dropped. False means the journal has no sent copy at the reference.
        """
        deleted = self._database.connection.execute(
            """
            UPDATE messages SET delivery = ?, body = NULL, refusal = ?
            WHERE platform_message_id = ? AND direction = ? AND delivery = ?
            """,
            (Delivery.DELETE_ATTEMPTED, reason, message_ref, Direction.OUT, Delivery.SENT),
        )
        return deleted.rowcount == 1

    def confirm_deletion(self, message_ref: str) -> None:
        self._database.connection.execute(
            """
            UPDATE messages SET delivery = ?
            WHERE platform_message_id = ? AND direction = ? AND delivery = ?
            """,
            (Delivery.DELETED, message_ref, Direction.OUT, Delivery.DELETE_ATTEMPTED),
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
