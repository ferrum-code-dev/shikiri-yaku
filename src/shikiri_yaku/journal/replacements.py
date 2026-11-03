"""The copies a newer copy of their slot replaced, and the journal of their deletion.

A copy is superseded when a newer copy of the same slot, signal, chat and thread has been sent. Its
deletion is journaled before the platform is asked. A copy the platform will not delete keeps its
text and is edited to the line that says it was replaced; a transient failure puts it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.journal.final_edits import final_copy
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.messages import MessageStore

_BODY = TypeAdapter(Message)


class JournalReplacements:
    """The journal side of the Replacements port: supersession is computed here, in SQL.

    Only copies that show as live are superseded: a sent copy with a slot, a body and no final
    state. A copy whose final state is already journaled is finished by the CopyEditor instead.
    """

    def __init__(self, database: Database, messages: MessageStore) -> None:
        self._database = database
        self._messages = messages

    def superseded(self) -> list[str]:
        rows = self._database.connection.execute(
            """
            SELECT platform_message_id FROM messages AS old
            WHERE old.direction = :out AND old.delivery = :sent AND old.slot IS NOT NULL
              AND old.body IS NOT NULL AND old.final_body IS NULL
              AND EXISTS (
                SELECT 1 FROM messages AS newer
                WHERE newer.id > old.id AND newer.direction = :out AND newer.delivery = :sent
                  AND newer.slot = old.slot AND newer.signal_number IS old.signal_number
                  AND newer.channel = old.channel AND newer.chat = old.chat
                  AND newer.thread IS old.thread
              )
            ORDER BY old.id
            """,
            {"out": Direction.OUT, "sent": Delivery.SENT},
        ).fetchall()
        return [row["platform_message_id"] for row in rows]

    def begin_deletion(self, message_ref: str) -> None:
        self._database.connection.execute(
            "UPDATE messages SET delivery = ? WHERE platform_message_id = ? AND direction = ? "
            "AND delivery = ?",
            (Delivery.DELETE_ATTEMPTED, message_ref, Direction.OUT, Delivery.SENT),
        )

    def keep_replaced(self, message_ref: str, line: str, reason: str) -> None:
        """Keep the copy as its final state, with the line and no buttons, and no slot to replace.

        The slot is cleared so the copy is never offered for deletion again, and the reason of the
        refusal stays on it. No PO notice is queued: nothing the PO sent or saw was refused.
        """
        connection = self._database.connection
        with self._database.transaction():
            row = connection.execute(
                "SELECT id, body FROM messages WHERE platform_message_id = ? AND direction = ? "
                "AND delivery = ?",
                (message_ref, Direction.OUT, Delivery.DELETE_ATTEMPTED),
            ).fetchone()
            if row is None:
                return
            final = final_copy(_BODY.validate_json(row["body"]), line)
            connection.execute(
                """
                UPDATE messages
                SET delivery = ?, slot = NULL, final_body = ?, refusal = COALESCE(refusal, ?)
                WHERE id = ?
                """,
                (Delivery.SENT, _BODY.dump_json(final).decode(), reason, row["id"]),
            )

    def undo_deletion(self, message_ref: str) -> None:
        """Put a copy back to sent after a transient failure, so the next tick tries again.

        Only a copy this journal began to replace is put back: it has a slot and its body is kept.
        A copy whose body a secret deletion dropped is left as that path recorded it.
        """
        self._database.connection.execute(
            """
            UPDATE messages SET delivery = ?
            WHERE platform_message_id = ? AND direction = ? AND delivery = ?
              AND slot IS NOT NULL AND body IS NOT NULL
            """,
            (Delivery.SENT, message_ref, Direction.OUT, Delivery.DELETE_ATTEMPTED),
        )

    def confirm_deletion(self, message_ref: str) -> None:
        self._messages.confirm_deletion(message_ref)
