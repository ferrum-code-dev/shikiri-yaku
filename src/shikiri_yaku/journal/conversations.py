"""Durable Expert conversations and their turns."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.expert import (
    MODE_EXPERT,
    ConversationRecord,
    ExpertSession,
    TurnRecord,
)
from shikiri_yaku.domain.place import Place
from shikiri_yaku.journal.database import parse_stamp, stamp
from shikiri_yaku.journal.final_edits import settle_notice
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from shikiri_yaku.journal.database import Database

_SCOPE = TypeAdapter(list[str])
_TURN_SELECT: Final = """
    SELECT t.update_id, t.conversation_id, c.room, t.text, t.state, t.answer
    FROM turns t JOIN conversations c ON c.id = t.conversation_id
"""
_OWNED_UPDATES: Final = """
    SELECT update_id FROM messages
    WHERE (conversation_id = :id OR update_id = :update_id OR
           (channel = :room_channel AND place = :room_place))
        AND update_id IS NOT NULL
"""
# keep: only these reply keys end in an event's id; other keys end in numbers an id may equal
_EVENT_REPLIES: Final = (
    "command",
    "submit:usage",
    "admin:decision",
    "expert:command",
    "expert:files",
    "expert:open",
    "expert:proposal",
    "expert:refusal",
    "expert:select",
    "expert:view",
)
_ERASE_MESSAGES: Final = """
    DELETE FROM messages
    WHERE conversation_id = :id OR update_id = :update_id OR
          (channel = :room_channel AND place = :room_place)
"""


def _record(row: sqlite3.Row) -> ConversationRecord:
    session = row["session"]
    return ConversationRecord(
        id=row["id"],
        event_id=row["update_id"],
        origin=Place(row["origin_channel"], row["origin_place"]),
        room=row["room"],
        scope=_SCOPE.validate_json(row["scope"]),
        state=row["state"],
        effort=row["effort"],
        session=None if session is None else ExpertSession(**json.loads(session)),
        ending_at=parse_stamp(row["ending_at"]),
        mode=row["mode"],
    )


def _turn(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        event_id=row["update_id"],
        conversation_id=row["conversation_id"],
        room=row["room"],
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
        event_id: int,
        *,
        origin: Place,
        text: str,
        scope: list[str],
        now: datetime,
        mode: str = MODE_EXPERT,
    ) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO conversations
                    (update_id, origin_channel, origin_place, scope, state, last_activity, mode)
                VALUES (?, ?, ?, ?, 'creating', ?, ?) ON CONFLICT(update_id) DO NOTHING
                """,
                (event_id, origin.channel, origin.id, json.dumps(scope), stamp(now), mode),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO turns (conversation_id, update_id, text, state)
                VALUES (?, ?, ?, 'pending')
                """,
                (cursor.lastrowid, event_id, text),
            )
            return True

    def add_turn(self, conversation_id: int, event_id: int, text: str, now: datetime) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO turns (conversation_id, update_id, text, state)
                VALUES (?, ?, ?, 'pending') ON CONFLICT(update_id) DO NOTHING
                """,
                (conversation_id, event_id, text),
            )
            connection.execute(
                "UPDATE conversations SET last_activity = ? WHERE id = ?",
                (stamp(now), conversation_id),
            )
            return cursor.rowcount == 1

    def refuse_admin(self, conversation_id: int, event_id: int, now: datetime) -> int | None:
        """Record a refused proposal without its text and schedule the Expert's next turn."""
        with self._database.transaction() as connection:
            inserted = connection.execute(
                "INSERT INTO ops (identity, kind, project, reason, state, created_at, updated_at) "
                "VALUES (?, 'admin', '', 'proposal refused', 'failed', ?, ?) "
                "ON CONFLICT(identity) DO NOTHING",
                (f"admin:refused:{event_id}", stamp(now), stamp(now)),
            ).rowcount
            if not inserted:
                return None
            row = connection.execute("SELECT MIN(update_id) FROM turns").fetchone()
            next_id = min(-1, row[0] - 1)
            connection.execute(
                "INSERT INTO turns (conversation_id, update_id, text, state) "
                "VALUES (?, ?, ?, 'pending')",
                (
                    conversation_id,
                    next_id,
                    "The proposed command was refused because it cannot be shown safely.",
                ),
            )
            return next_id

    def get(self, event_id: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE update_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else _record(row)

    def conversation(self, conversation_id: int) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return None if row is None else _record(row)

    def at_room(self, room: str) -> ConversationRecord | None:
        row = self._database.connection.execute(
            "SELECT * FROM conversations WHERE room = ? AND state = 'open'",
            (room,),
        ).fetchone()
        return None if row is None else _record(row)

    def turn(self, event_id: int) -> TurnRecord | None:
        row = self._database.connection.execute(
            f"{_TURN_SELECT} WHERE t.update_id = ?", (event_id,)
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

    def pending_admin_followups(self) -> list[tuple[int, int]]:
        """Return generated admin turns left pending at startup in their original order."""
        rows = self._database.connection.execute(
            "SELECT t.conversation_id, t.update_id FROM turns t "
            "JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.mode = 'admin' AND c.state = 'open' AND t.state = 'pending' "
            "AND t.update_id < 0 ORDER BY t.id"
        ).fetchall()
        return [(row["conversation_id"], row["update_id"]) for row in rows]

    def followups(self, conversation_id: int) -> list[TurnRecord]:
        """Return the generated turns of a conversation, pending or done, in the order made."""
        rows = self._database.connection.execute(
            f"""
            {_TURN_SELECT}
            WHERE t.conversation_id = ? AND t.update_id < 0 AND t.state IN ('pending', 'done')
            ORDER BY t.id
            """,
            (conversation_id,),
        ).fetchall()
        return [_turn(row) for row in rows]

    def history(self, conversation_id: int, *, before: int) -> list[TurnRecord]:
        rows = self._database.connection.execute(
            f"""
            {_TURN_SELECT}
            JOIN turns later ON later.update_id = ?
            WHERE t.conversation_id = ? AND t.state = 'done' AND t.id < later.id
            ORDER BY t.id
            """,
            (before, conversation_id),
        ).fetchall()
        return [_turn(row) for row in rows]

    def set_effort(self, conversation_id: int, effort: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET effort = ? WHERE id = ?", (effort, conversation_id)
            )

    def fail(self, event_id: int) -> None:
        self._database.connection.execute(
            "UPDATE conversations SET state = 'failed' WHERE update_id = ?", (event_id,)
        )

    def opened(self, event_id: int, room: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET state = 'open', room = ? WHERE update_id = ?",
                (room, event_id),
            )

    def start(self, event_id: int) -> bool:
        cursor = self._database.connection.execute(
            """
            UPDATE turns SET state = 'running'
            WHERE update_id = ? AND state = 'pending'
                AND conversation_id IN (SELECT id FROM conversations WHERE state = 'open')
            """,
            (event_id,),
        )
        return cursor.rowcount == 1

    def finish(
        self,
        event_id: int,
        answer: str,
        session: str | None,
        call: str | None,
        now: datetime,
        *,
        clear_session: bool = False,
        refused: bool = False,
    ) -> None:
        # keep: a turn that failed before the agent answered keeps the session of the turns before
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE turns SET state = 'done', answer = ?, call = ? WHERE update_id = ?",
                (answer, call, event_id),
            )
            connection.execute(
                """
                UPDATE conversations SET
                    session = CASE WHEN ? THEN NULL ELSE COALESCE(?, session) END,
                    last_activity = ?
                WHERE id = (SELECT conversation_id FROM turns WHERE update_id = ?)
                """,
                (clear_session, session, stamp(now), event_id),
            )
            if refused:
                row = connection.execute(
                    "SELECT conversation_id FROM turns WHERE update_id = ?", (event_id,)
                ).fetchone()
                if row is not None:
                    self.refuse_admin(row["conversation_id"], event_id, now)

    def touch(self, conversation_id: int, now: datetime) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET last_activity = ? WHERE id = ?",
                (stamp(now), conversation_id),
            )

    def begin_ending(
        self,
        conversation_id: int,
        now: datetime,
        *,
        sender: str | None = None,
        event_id: int | None = None,
    ) -> ConversationRecord | None:
        """Mark an open conversation as ending and keep that first time for recovery."""
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE conversations SET state = 'closing', ending_at = COALESCE(ending_at, ?)
                WHERE id = ? AND state IN ('open', 'closing')""",
                (stamp(now), conversation_id),
            )
            voided = connection.execute(
                "UPDATE ops SET state = 'void', updated_at = ?, "
                "payload = json_set(payload, '$.approver', NULL, '$.actor', ?, '$.update_id', ?) "
                "WHERE kind = 'admin' AND state = 'intended' "
                "AND json_extract(payload, '$.conversation_id') = ? RETURNING id",
                (stamp(now), sender, event_id, conversation_id),
            ).fetchall()
            for op in voided:
                settle_notice(connection, f"admin:proposal:{op['id']}", Message("Proposal voided."))
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return None if row is None else _record(row)

    def idle_before(self, cutoff: datetime, mode: str = MODE_EXPERT) -> list[ConversationRecord]:
        """Return the open conversations of a mode whose last message is older than the cutoff."""
        rows = self._database.connection.execute(
            "SELECT * FROM conversations WHERE state = 'open' AND mode = ? AND last_activity < ?",
            (mode, stamp(cutoff)),
        ).fetchall()
        return [_record(row) for row in rows]

    def ending(self) -> list[ConversationRecord]:
        """Return the conversations whose deletion has not succeeded yet."""
        rows = self._database.connection.execute(
            "SELECT * FROM conversations WHERE state = 'closing' ORDER BY id"
        ).fetchall()
        return [_record(row) for row in rows]

    def erase(self, conversation_id: int, *, room_place: Place) -> None:
        """Delete every row of a conversation.

        That is its messages and replies, turns, request, and its proposals with their tasks, whose
        request texts are the conversation's own.

        A message is the conversation's when it is tagged with it, is its request, or was sent in
        its room. A channel does not reuse a room's id, so the room's place picks out only the
        messages of this conversation.
        """
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT update_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return
            owned = {
                "id": conversation_id,
                "update_id": row["update_id"],
                "room_channel": room_place.channel,
                "room_place": room_place.id,
            }
            updates = connection.execute(_OWNED_UPDATES, owned).fetchall()
            connection.executemany(
                "DELETE FROM command_replies WHERE reply_key = ?",
                [
                    (f"{reply}:{update['update_id']}",)
                    for update in updates
                    for reply in _EVENT_REPLIES
                ],
            )
            # keep: a reply part's key is its reply key and index, read before its messages go
            notices = connection.execute(
                "SELECT notice_key FROM messages "
                "WHERE conversation_id = ? AND notice_key IS NOT NULL",
                (conversation_id,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM command_replies WHERE reply_key = ?",
                [(notice["notice_key"].rpartition(":")[0],) for notice in notices],
            )
            connection.execute(_ERASE_MESSAGES, owned)
            # keep: an earlier version wrote such a row, and its reference blocks the erasure
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
