"""Durable Expert conversations and their turns."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.expert import ConversationRecord, ExpertSession, TurnRecord
from shikiri_yaku.journal.database import stamp

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from shikiri_yaku.journal.database import Database

_SCOPE = TypeAdapter(list[str])
_TURN_SELECT: Final = """
    SELECT t.update_id, t.conversation_id, c.thread_id, t.text, t.state, t.answer
    FROM turns t JOIN conversations c ON c.id = t.conversation_id
"""
_OWNED_UPDATES: Final = """
    SELECT update_id FROM messages
    WHERE (conversation_id = :id OR update_id = :update_id OR
           (chat = :topic_chat AND thread = :thread_id))
        AND update_id IS NOT NULL
"""
_ERASE_MESSAGES: Final = """
    DELETE FROM messages
    WHERE conversation_id = :id OR update_id = :update_id OR
          (chat = :topic_chat AND thread = :thread_id)
"""


def _record(row: sqlite3.Row) -> ConversationRecord:
    session = row["session"]
    return ConversationRecord(
        id=row["id"],
        update_id=row["update_id"],
        chat=row["chat"],
        origin_thread=row["origin_thread"],
        thread_id=row["thread_id"],
        scope=_SCOPE.validate_json(row["scope"]),
        state=row["state"],
        effort=row["effort"],
        session=None if session is None else ExpertSession(**json.loads(session)),
    )


def _turn(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        update_id=row["update_id"],
        conversation_id=row["conversation_id"],
        thread_id=row["thread_id"],
        text=row["text"],
        state=row["state"],
        answer=row["answer"],
    )


class Conversations:
    """Store each request and each turn before its topic or agent work can start."""

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

    def add_turn(self, conversation_id: int, update_id: int, text: str, now: datetime) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO turns (conversation_id, update_id, text, state)
                VALUES (?, ?, ?, 'pending') ON CONFLICT(update_id) DO NOTHING
                """,
                (conversation_id, update_id, text),
            )
            connection.execute(
                "UPDATE conversations SET last_activity = ? WHERE id = ?",
                (stamp(now), conversation_id),
            )
            return cursor.rowcount == 1

    def get(self, update_id: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE update_id = ?", (update_id,)
        ).fetchone()
        return None if row is None else _record(row)

    def conversation(self, conversation_id: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return None if row is None else _record(row)

    def at_thread(self, thread: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE thread_id = ? AND state = 'open'",
            (thread,),
        ).fetchone()
        return None if row is None else _record(row)

    def turn(self, update_id: int) -> TurnRecord | None:
        row = self._database.connection.execute(
            f"{_TURN_SELECT} WHERE t.update_id = ?", (update_id,)
        ).fetchone()
        return None if row is None else _turn(row)

    def oldest_pending(self, conversation_id: int) -> TurnRecord | None:
        row = self._database.connection.execute(
            f"""
            {_TURN_SELECT}
            WHERE t.conversation_id = ? AND t.state = 'pending'
            ORDER BY t.update_id LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return None if row is None else _turn(row)

    def history(self, conversation_id: int, *, before: int) -> list[TurnRecord]:
        rows = self._database.connection.execute(
            f"""
            {_TURN_SELECT}
            WHERE t.conversation_id = ? AND t.state = 'done' AND t.update_id < ?
            ORDER BY t.update_id
            """,
            (conversation_id, before),
        ).fetchall()
        return [_turn(row) for row in rows]

    def set_effort(self, conversation_id: int, effort: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET effort = ? WHERE id = ?", (effort, conversation_id)
            )

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
            """
            UPDATE turns SET state = 'running'
            WHERE update_id = ? AND state = 'pending'
                AND conversation_id IN (SELECT id FROM conversations WHERE state = 'open')
            """,
            (update_id,),
        )
        return cursor.rowcount == 1

    def finish(
        self,
        update_id: int,
        answer: str,
        session: str | None,
        call: str | None,
        now: datetime,
    ) -> None:
        # keep: a turn that failed before the agent answered keeps the session of the turns before
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE turns SET state = 'done', answer = ?, call = ? WHERE update_id = ?",
                (answer, call, update_id),
            )
            connection.execute(
                """
                UPDATE conversations SET session = COALESCE(?, session), last_activity = ?
                WHERE id = (SELECT conversation_id FROM turns WHERE update_id = ?)
                """,
                (session, stamp(now), update_id),
            )

    def touch(self, conversation_id: int, now: datetime) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET last_activity = ? WHERE id = ?",
                (stamp(now), conversation_id),
            )

    def begin_ending(self, conversation_id: int) -> bool:
        """Mark an open conversation as ending; no new turn or message reaches it after this."""
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET state = 'closing' WHERE id = ? AND state = 'open'",
                (conversation_id,),
            )
            return cursor.rowcount == 1

    def idle_before(self, cutoff: datetime) -> list[ConversationRecord]:
        """Return the open conversations whose last message is older than the cutoff."""
        rows = self._database.connection.execute(
            "SELECT * FROM conversations WHERE state = 'open' AND last_activity < ?",
            (stamp(cutoff),),
        ).fetchall()
        return [_record(row) for row in rows]

    def ending(self) -> list[ConversationRecord]:
        """Return the conversations whose deletion has not succeeded yet."""
        rows = self._database.connection.execute(
            "SELECT * FROM conversations WHERE state = 'closing' ORDER BY id"
        ).fetchall()
        return [_record(row) for row in rows]

    def erase(self, conversation_id: int, *, topic_chat: int) -> None:
        """Delete every row of a conversation.

        That is its messages and replies, topic, turns, request, and its proposals with their tasks,
        whose request texts are the conversation's own.

        A message is the conversation's when it is tagged with it, is its request, or was sent in
        its topic. A channel does not reuse a topic's id in its chat, so the topic picks out only
        the messages of this conversation.
        """
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT update_id, thread_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return
            owned = {
                "id": conversation_id,
                "update_id": row["update_id"],
                "topic_chat": str(topic_chat),
                "thread_id": row["thread_id"],
            }
            updates = connection.execute(_OWNED_UPDATES, owned).fetchall()
            connection.executemany(
                "DELETE FROM command_replies WHERE reply_key LIKE '%:' || ?",
                [(update["update_id"],) for update in updates],
            )
            connection.execute(_ERASE_MESSAGES, owned)
            connection.execute("DELETE FROM topics WHERE conversation_id = ?", (conversation_id,))
            connection.execute(
                "DELETE FROM ops WHERE kind = 'submit' AND json_extract(payload, '$.proposal')"
                " IN (SELECT update_id FROM proposals WHERE conversation_id = ?)",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM proposals WHERE conversation_id = ?", (conversation_id,)
            )
            connection.execute(
                "DELETE FROM web_requests WHERE update_id IN "
                "(SELECT update_id FROM turns WHERE conversation_id = ?)",
                (conversation_id,),
            )
            connection.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def recover_creations(self) -> list[ConversationRecord]:
        with self._database.transaction() as connection:
            connection.execute("UPDATE conversations SET state = 'failed' WHERE state = 'creating'")
            rows = connection.execute(
                "SELECT * FROM conversations WHERE state = 'failed'"
            ).fetchall()
            return [_record(row) for row in rows]

    def recover_turns(self) -> list[TurnRecord]:
        with self._database.transaction() as connection:
            connection.execute("UPDATE turns SET state = 'interrupted' WHERE state = 'running'")
            rows = connection.execute(f"{_TURN_SELECT} WHERE t.state = 'interrupted'").fetchall()
            return [_turn(row) for row in rows]
