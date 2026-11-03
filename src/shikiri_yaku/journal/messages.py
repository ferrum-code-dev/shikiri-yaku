from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose, message_label, refusal_notice
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.journal.database import Inserted, stamp
from shikiri_yaku.journal.signals import SignalState
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
    HANDLED = "handled"
    DELETE_ATTEMPTED = "delete_attempted"
    DELETED = "deleted"
    OBSOLETE = "obsolete"


_BODY = TypeAdapter(Message)


@dataclass(frozen=True)
class StoredMessage:
    id: int
    signal_number: int | None
    channel: str
    chat: str
    thread: int | None
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
        thread=row["thread"],
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
        signal_number: int | None,
        channel: str,
        chat: str,
        thread: int | None = None,
        purpose: Purpose,
        body: Message,
        notice_key: str | None,
        now: datetime,
        slot: str | None = None,
        standing: bool = False,
    ) -> Inserted:
        """Journal a message and its body before it is sent; a key queued for that chat is known.

        A body that carries a secret is refused here: its form is kept, its text is not, and the PO
        gets the notice of the refusal. A standing copy keeps its reminders until the PO stops them.
        """
        form = secret_form(body.texts())
        with self._database.transaction():
            inserted = self._insert(
                signal_number=signal_number,
                channel=channel,
                chat=chat,
                thread=thread,
                purpose=purpose,
                notice_key=notice_key,
                now=now,
                body=None if form is not None else _BODY.dump_json(body).decode(),
                refusal=form,
                slot=slot,
                standing=standing,
            )
            if form is not None and inserted.is_new:
                self._notify(
                    signal_number=signal_number,
                    channel=channel,
                    chat=chat,
                    thread=thread,
                    refused_id=inserted.row_id,
                    reason=form,
                    now=now,
                )
        return inserted

    def _insert(
        self,
        *,
        signal_number: int | None,
        channel: str,
        chat: str,
        thread: int | None,
        purpose: Purpose,
        notice_key: str | None,
        now: datetime,
        body: str | None,
        refusal: str | None,
        slot: str | None = None,
        standing: bool = False,
    ) -> Inserted:
        """Insert an outbound row, or find the row that has its notice key for that chat."""
        connection = self._database.connection
        delivery = Delivery.PENDING if refusal is None else Delivery.REFUSED
        created = connection.execute(
            """
            INSERT INTO messages (
                signal_number, direction, channel, chat, thread, purpose, notice_key,
                delivery, body, refusal, created_at, slot, standing
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (notice_key, channel, chat, COALESCE(thread, -1)) DO NOTHING
            """,
            (
                signal_number,
                Direction.OUT,
                channel,
                chat,
                thread,
                purpose,
                notice_key,
                delivery,
                body,
                refusal,
                stamp(now),
                slot,
                standing,
            ),
        )
        if created.rowcount == 1:
            return Inserted(created.lastrowid or 0, is_new=True)
        row = connection.execute(
            """
            SELECT id FROM messages
            WHERE notice_key = ? AND channel = ? AND chat = ? AND thread IS ?
            """,
            (notice_key, channel, chat, thread),
        ).fetchone()
        return Inserted(row["id"], is_new=False)

    def _notify(
        self,
        *,
        signal_number: int | None,
        channel: str,
        chat: str,
        thread: int | None,
        refused_id: int,
        reason: str,
        now: datetime,
    ) -> None:
        """Queue the PO's notice of a refusal; if it cannot be journaled, report its metadata only.

        The notice is a fixed text with the S-number and no refused content. It is keyed by the
        refused row, so a refusal is noticed once per chat.
        """
        notice = Message(refusal_notice(signal_number))
        try:
            self._insert(
                signal_number=signal_number,
                channel=channel,
                chat=chat,
                thread=thread,
                purpose=Purpose.NOTICE,
                notice_key=f"{REFUSAL_KEY_PREFIX}{refused_id}",
                now=now,
                body=_BODY.dump_json(notice).decode(),
                refusal=None,
            )
        except sqlite3.Error:
            self._reporter.report(
                f"{message_label(signal_number)} refused ({reason}); its notice was not journaled"
            )

    def _notify_refusal(self, row: sqlite3.Row, reason: str, now: datetime) -> None:
        """Notify the PO of a refused row; a refused notice is reported instead, never noticed."""
        if _is_notice_of_refusal(row["notice_key"]):
            self._reporter.report(
                f"{message_label(row['signal_number'])} notice refused ({reason})"
            )
            return
        self._notify(
            signal_number=row["signal_number"],
            channel=row["channel"],
            chat=row["chat"],
            thread=row["thread"],
            refused_id=row["id"],
            reason=reason,
            now=now,
        )

    def confirm_delivery(self, message_id: int, platform_message_id: str) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ?, platform_message_id = ? WHERE id = ?",
            (Delivery.SENT, platform_message_id, message_id),
        )

    def refuse_delivery(self, message_id: int, reason: str, now: datetime) -> None:
        """Drop the body of a message the platform refused for good; only the reason stays."""
        connection = self._database.connection
        with self._database.transaction():
            row = connection.execute(
                """
                SELECT id, signal_number, channel, chat, thread, notice_key FROM messages
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
            self._notify_refusal(row, reason, now)

    def record_refused_edit(self, message_ref: str, reason: str, *, now: datetime) -> None:
        """Keep the reason of a refused edit on the copy it targeted, and notify the PO of it."""
        self._refuse_copy(message_ref, reason, now=now, delete=False)

    def record_deletion(self, message_ref: str, reason: str, *, now: datetime) -> bool:
        """Journal an attempt to delete the copy at this reference, before it is invoked.

        The body of the copy is dropped. False means the journal has no sent copy at the reference.
        """
        return self._refuse_copy(message_ref, reason, now=now, delete=True)

    def _refuse_copy(self, message_ref: str, reason: str, *, now: datetime, delete: bool) -> bool:
        """Mark the sent copy at this reference as refused, and notify the PO; False if unknown."""
        connection = self._database.connection
        with self._database.transaction():
            row = connection.execute(
                """
                SELECT id, signal_number, channel, chat, thread, notice_key FROM messages
                WHERE platform_message_id = ? AND direction = ? AND delivery = ?
                """,
                (message_ref, Direction.OUT, Delivery.SENT),
            ).fetchone()
            if row is None:
                return False
            if delete:
                connection.execute(
                    "UPDATE messages SET delivery = ?, body = NULL, refusal = ? WHERE id = ?",
                    (Delivery.DELETE_ATTEMPTED, reason, row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE messages SET refusal = ? WHERE id = ?", (reason, row["id"])
                )
            self._notify_refusal(row, reason, now)
        return True

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

    def unconfirmed_message(self, message_id: int) -> StoredMessage | None:
        row = self._database.connection.execute(
            """
            SELECT * FROM messages
            WHERE id = ? AND direction = ? AND delivery = ? AND body IS NOT NULL
            """,
            (message_id, Direction.OUT, Delivery.PENDING),
        ).fetchone()
        return None if row is None else _message(row)

    def retire_obsolete(self, ending_prefixes: tuple[str, ...]) -> None:
        """Mark unsent copies of an ending notice obsolete once their signal has closed.

        A notice the signal ends, such as a health message, reports a state that is over by then,
        so sending it would tell the PO something false. The row keeps its body and is never sent.
        """
        self._database.connection.execute(
            """
            UPDATE messages SET delivery = :obsolete
            WHERE direction = :out AND delivery = :pending AND body IS NOT NULL
                AND signal_number IN (
                    SELECT number FROM signals WHERE state IN (:resolved, :closed)
                )
                AND EXISTS (
                    SELECT 1 FROM json_each(:prefixes) AS prefix
                    WHERE substr(messages.notice_key, 1, length(prefix.value)) = prefix.value
                )
            """,
            {
                "obsolete": Delivery.OBSOLETE,
                "out": Direction.OUT,
                "pending": Delivery.PENDING,
                "resolved": SignalState.RESOLVED,
                "closed": SignalState.CLOSED,
                "prefixes": json.dumps(ending_prefixes),
            },
        )

    def for_signal(self, signal_number: int) -> list[StoredMessage]:
        rows = self._database.connection.execute(
            "SELECT * FROM messages WHERE signal_number = ? AND direction = ? ORDER BY id",
            (signal_number, Direction.OUT),
        )
        return [_message(row) for row in rows]
