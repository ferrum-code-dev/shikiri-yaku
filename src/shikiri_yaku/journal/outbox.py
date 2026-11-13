from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.journal.messages import Delivery
from shikiri_yaku.ports import Pending

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.messages import MessageStore
    from shikiri_yaku.ports import Message


class JournalOutbox:
    """The outbound rows of the messages table: body and destination are stored before any send.

    It is also the Copies of the Channel: refused edits and deletions are journaled on the same
    rows.
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
        destinations: Sequence[Destination],
        message: Message,
        *,
        key: str,
        now: datetime,
        slot: str | None = None,
        standing: bool = False,
        conversation_id: int | None = None,
    ) -> None:
        # keep: one transaction, so an interruption leaves every destination or none
        with self._database.transaction():
            for where in destinations:
                self._messages.record_outbound(
                    signal_number=number,
                    channel=where.channel,
                    chat=where.chat,
                    thread=where.thread,
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

    def queue_command_reply(
        self,
        destinations: Sequence[Destination],
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
                for where in destinations:
                    self._messages.record_outbound(
                        signal_number=None,
                        channel=where.channel,
                        chat=where.chat,
                        thread=where.thread,
                        purpose=Purpose.NOTICE,
                        body=part,
                        notice_key=f"{key}:{index}",
                        now=now,
                        conversation_id=conversation_id,
                    )

    def expert_selection(self, channel: str, item: Inbound) -> Inbound | None:
        row = self._database.connection.execute(
            """SELECT original.* FROM messages AS offered
            JOIN messages AS original
              ON offered.notice_key = 'ask:select:' || original.update_id || ':0'
            WHERE offered.direction = 'out' AND offered.delivery = 'sent'
              AND offered.channel = ? AND offered.chat = ? AND offered.platform_message_id = ?
              AND original.direction = 'in' AND original.kind = 'text'
              AND ? = 'expert:' || original.update_id""",
            (channel, str(item.chat), item.message_ref, item.data),
        ).fetchone()
        if row is None:
            return None
        return Inbound(
            row["update_id"],
            InboundKind.TEXT,
            row["sender"],
            int(row["chat"]),
            row["message_ref"],
            text=row["body"],
            thread=row["thread"],
        )

    def pending(self) -> list[Pending]:
        self._messages.retire_obsolete(self._endings)
        return [
            Pending(
                stored.id,
                Destination(stored.channel, stored.chat, stored.thread),
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
            Destination(stored.channel, stored.chat, stored.thread),
            stored.body,
            stored.conversation_id,
        )

    def _ready(self, message_id: int) -> bool:
        pending = self._database.connection.execute(
            "SELECT notice_key, final_body, channel, chat, thread FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if pending is None:
            return False
        notice_key = pending["notice_key"]
        if (
            notice_key is None
            or not notice_key.startswith("ask:answer:")
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
            "SELECT delivery FROM messages WHERE channel = ? AND chat = ? AND thread IS ?"
            " AND (notice_key = ? OR notice_key LIKE ?)",
            (
                pending["channel"],
                pending["chat"],
                pending["thread"],
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

    def record_refused_edit(self, message_ref: str, reason: str, *, now: datetime) -> None:
        self._messages.record_refused_edit(message_ref, reason, now=now)

    def record_deletion(self, message_ref: str, reason: str, *, now: datetime) -> bool:
        return self._messages.record_deletion(message_ref, reason, now=now)

    def confirm_deletion(self, message_ref: str) -> None:
        self._messages.confirm_deletion(message_ref)
