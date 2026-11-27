from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.place import Place
from shikiri_yaku.journal.messages import Delivery
from shikiri_yaku.ports import Pending

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.messages import MessageStore
    from shikiri_yaku.ports import Message


class JournalOutbox:
    """The outbound rows of the messages table: body and place are stored before any send.

    It is also the Copies the CopyEditor journals refused edits and deletions on, on the same rows.
    """

    def __init__(
        self,
        database: Database,
        messages: MessageStore,
        *,
        ending_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._database = database
        self._messages = messages
        self._endings = ending_prefixes

    def queue(
        self,
        number: int | None,
        purpose: Purpose,
        places: Sequence[Place],
        message: Message,
        *,
        key: str,
        now: datetime,
        slot: str | None = None,
        standing: bool = False,
        conversation_id: int | None = None,
    ) -> None:
        # keep: one transaction, so an interruption leaves every place or none
        with self._database.transaction():
            for where in places:
                self._messages.record_outbound(
                    signal_number=number,
                    place=where,
                    purpose=purpose,
                    body=message,
                    notice_key=key,
                    now=now,
                    slot=slot,
                    standing=standing,
                    conversation_id=conversation_id,
                )

    def command_reply_queued(self, key: str) -> bool:
        row = self._database.connection.execute(
            "SELECT 1 FROM command_replies WHERE reply_key = ?", (key,)
        ).fetchone()
        return row is not None

    def notice_queued(self, key: str) -> bool:
        row = self._database.connection.execute(
            "SELECT 1 FROM messages WHERE direction = 'out' AND notice_key = ?", (key,)
        ).fetchone()
        return row is not None

    def settled(self, key: str) -> bool:
        row = self._database.connection.execute(
            "SELECT delivery FROM messages WHERE direction = 'out' AND notice_key = ?", (key,)
        ).fetchone()
        return row is not None and row["delivery"] != Delivery.PENDING

    def sent(self, key: str) -> bool:
        row = self._database.connection.execute(
            "SELECT delivery FROM messages WHERE direction = 'out' AND notice_key = ?", (key,)
        ).fetchone()
        return row is not None and row["delivery"] == Delivery.SENT

    def recover_interrupted_drawings(self, message: Message, now: datetime) -> None:
        # keep: retire unsent progress and queue each notice in one commit, so a restart can retry
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT progress.id, progress.notice_key, progress.delivery,
                    SUBSTR(progress.notice_key, 1, LENGTH(progress.notice_key) - 9) AS sketch_key,
                    progress.channel, progress.place, progress.conversation_id
            FROM messages AS progress
            WHERE progress.direction = 'out'
              AND progress.delivery IN (?, ?)
              AND progress.notice_key LIKE 'expert:sketch:%:progress'
              AND NOT EXISTS (
                SELECT 1 FROM messages AS sketch
                WHERE sketch.direction = 'out'
                  AND sketch.notice_key || ':progress' = progress.notice_key
              )""",
                (Delivery.PENDING, Delivery.SENT),
            ).fetchall()
            for row in rows:
                if row["delivery"] == Delivery.PENDING:
                    connection.execute(
                        "UPDATE messages SET delivery = ? WHERE id = ? AND delivery = ?",
                        (Delivery.OBSOLETE, row["id"], Delivery.PENDING),
                    )
                self._messages.record_outbound(
                    signal_number=None,
                    place=Place(row["channel"], row["place"]),
                    purpose=Purpose.EXPERT,
                    body=message,
                    notice_key=row["sketch_key"],
                    now=now,
                    slot=row["sketch_key"],
                    conversation_id=row["conversation_id"],
                )

    def queue_command_reply(
        self,
        places: Sequence[Place],
        parts: Sequence[Message],
        *,
        key: str,
        now: datetime,
        conversation_id: int | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            created = connection.execute(
                "INSERT INTO command_replies (reply_key) VALUES (?) ON CONFLICT DO NOTHING",
                (key,),
            )
            if created.rowcount != 1:
                return
            for index, part in enumerate(parts):
                for where in places:
                    self._messages.record_outbound(
                        signal_number=None,
                        place=where,
                        purpose=Purpose.NOTICE,
                        body=part,
                        notice_key=f"{key}:{index}",
                        now=now,
                        conversation_id=conversation_id,
                    )

    def expert_selection(self, item: Inbound) -> Inbound | None:
        row = self._database.connection.execute(
            """SELECT original.* FROM messages AS offered
            JOIN messages AS original
              ON offered.notice_key = 'expert:select:' || original.update_id || ':0'
            WHERE offered.direction = 'out' AND offered.delivery = 'sent'
              AND offered.channel = ? AND offered.platform_message_id = ?
              AND original.direction = 'in' AND original.kind IN ('text', 'reply')
              AND ? = 'expert:' || original.update_id""",
            (item.place.channel, item.message_ref, item.data),
        ).fetchone()
        if row is None:
            return None
        return Inbound(
            id=row["update_id"],
            key=row["event_key"],
            kind=InboundKind.TEXT,
            sender=row["sender"],
            place=Place(row["channel"], row["place"]),
            message_ref=row["message_ref"],
            text=row["body"],
        )

    def pending(self) -> list[Pending]:
        self._messages.retire_obsolete(self._endings)
        return [
            Pending(
                stored.id,
                stored.place,
                stored.body,
                stored.conversation_id,
            )
            for stored in self._messages.unconfirmed()
            if stored.body is not None and self._ready(stored.id)
        ]

    def pending_message(self, message_id: int) -> Pending | None:
        self._messages.retire_obsolete(self._endings)
        stored = self._messages.unconfirmed_message(message_id)
        if stored is None or stored.body is None or not self._ready(stored.id):
            return None
        return Pending(
            stored.id,
            stored.place,
            stored.body,
            stored.conversation_id,
        )

    def _ready(self, message_id: int) -> bool:
        pending = self._database.connection.execute(
            "SELECT notice_key, final_body, channel, place FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if pending is None:
            return False
        notice_key = pending["notice_key"]
        if (
            notice_key is None
            or not notice_key.startswith("expert:answer:")
            or not notice_key.endswith(":actions")
        ):
            return True
        if pending["final_body"] is not None:
            self._database.connection.execute(
                "UPDATE messages SET delivery = ? WHERE id = ? AND delivery = ?",
                (Delivery.OBSOLETE, message_id, Delivery.PENDING),
            )
            return False
        answer_key = notice_key.removesuffix(":actions")
        rows = self._database.connection.execute(
            "SELECT delivery FROM messages WHERE channel = ? AND place = ?"
            " AND (notice_key = ? OR notice_key LIKE ?)",
            (
                pending["channel"],
                pending["place"],
                answer_key,
                f"{answer_key}:part:%",
            ),
        ).fetchall()
        deliveries = [row["delivery"] for row in rows]
        if any(delivery in (Delivery.REFUSED, Delivery.OBSOLETE) for delivery in deliveries):
            self._database.connection.execute(
                "UPDATE messages SET delivery = ? WHERE id = ? AND delivery = ?",
                (Delivery.OBSOLETE, message_id, Delivery.PENDING),
            )
            return False
        return bool(deliveries) and all(delivery == Delivery.SENT for delivery in deliveries)

    def confirm(self, message_id: int, message_ref: str) -> None:
        self._messages.confirm_delivery(message_id, message_ref)

    def refuse(self, message_id: int, reason: str, *, now: datetime) -> None:
        self._messages.refuse_delivery(message_id, reason, now)

    def record_refused_edit(self, message_id: int, reason: str, *, now: datetime) -> None:
        self._messages.record_refused_edit(message_id, reason, now=now)

    def record_deletion(self, message_id: int, reason: str, *, now: datetime) -> bool:
        return self._messages.record_deletion(message_id, reason, now=now)

    def confirm_deletion(self, message_id: int) -> None:
        self._messages.confirm_deletion(message_id)
