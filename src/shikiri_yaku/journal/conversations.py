"""Durable creation and first turns of Expert conversations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.expert import ConversationRecord
from shikiri_yaku.journal.database import stamp

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from shikiri_yaku.journal.database import Database

_SCOPE = TypeAdapter(list[str])


def _record(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        id=row["id"],
        update_id=row["update_id"],
        chat=row["chat"],
        origin_thread=row["origin_thread"],
        thread_id=row["thread_id"],
        scope=_SCOPE.validate_json(row["scope"]),
        text=row["text"],
        state=row["state"],
        turn_state=row["turn_state"],
        answer=row["answer"],
    )


class Conversations:
    """Store each request before its topic or turn can be started."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        update_id: int,
        *,
        chat: int,
        thread: int | None,
        text: str,
        scope: list[str],
        now: datetime,
    ) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO conversations
                    (update_id, chat, origin_thread, scope, state, last_activity)
                VALUES (?, ?, ?, ?, 'creating', ?) ON CONFLICT(update_id) DO NOTHING
                """,
                (update_id, chat, thread, json.dumps(scope), stamp(now)),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO turns (conversation_id, update_id, text, state)
                VALUES (?, ?, ?, 'pending')
                """,
                (cursor.lastrowid, update_id, text),
            )
            return True

    def get(self, update_id: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            """
            SELECT c.*, t.text, t.state AS turn_state, t.answer FROM conversations c
            JOIN turns t ON t.conversation_id = c.id WHERE c.update_id = ?
            """,
            (update_id,),
        ).fetchone()
        return None if row is None else _record(row)

    def fail(self, update_id: int) -> None:
        self._database.connection.execute(
            "UPDATE conversations SET state = 'failed' WHERE update_id = ?", (update_id,)
        )

    def opened(self, update_id: int, thread: int) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET state = 'open', thread_id = ? WHERE update_id = ?",
                (thread, update_id),
            )
            connection.execute(
                """
                INSERT INTO topics (component, thread_id, state, conversation_id)
                SELECT NULL, ?, 'open', id FROM conversations WHERE update_id = ?
                """,
                (thread, update_id),
            )

    def start(self, update_id: int) -> bool:
        cursor = self._database.connection.execute(
            "UPDATE turns SET state = 'running' WHERE update_id = ? AND state = 'pending'",
            (update_id,),
        )
        return cursor.rowcount == 1

    def finish(self, update_id: int, answer: str, session: str, call: str | None) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE turns SET state = 'done', answer = ?, call = ? WHERE update_id = ?",
                (answer, call, update_id),
            )
            connection.execute(
                "UPDATE conversations SET session = ? WHERE update_id = ?", (session, update_id)
            )

    def recover(self) -> list[ConversationRecord]:
        with self._database.transaction() as connection:
            connection.execute("UPDATE conversations SET state = 'failed' WHERE state = 'creating'")
            connection.execute("UPDATE turns SET state = 'interrupted' WHERE state = 'running'")
            rows = connection.execute(
                """
                SELECT c.*, t.text, t.answer, t.state AS turn_state FROM conversations c
                JOIN turns t ON t.conversation_id = c.id
                WHERE c.state = 'failed' OR t.state = 'interrupted'
                """
            ).fetchall()
            return [_record(row) for row in rows]
