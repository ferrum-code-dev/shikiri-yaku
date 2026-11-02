from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
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

    def __init__(self, database: Database, messages: MessageStore) -> None:
        self._database = database
        self._messages = messages

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
                    )

    def pending(self) -> list[Pending]:
        return [
            Pending(
                stored.id,
                Destination(stored.channel, stored.chat, stored.thread),
                stored.body,
            )
            for stored in self._messages.unconfirmed()
            if stored.body is not None
        ]

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
