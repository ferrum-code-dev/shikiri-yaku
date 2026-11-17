"""Durable Expert web grants and requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.final_edits import settle_notice
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database


PENDING = "pending"
_ALLOWED = "allowed"
_DENIED = "denied"


def _decision_message(host: str, state: str) -> Message:
    return Message(f"Web access to {host} {state}.")


class JournalWeb:
    """Store grants and atomically turn one request decision into one Expert turn."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def granted(self, host: str) -> bool:
        return (
            self._database.connection.execute(
                "SELECT 1 FROM web_grants WHERE host = ?", (host,)
            ).fetchone()
            is not None
        )

    def request(self, update_id: int, host: str) -> None:
        self._database.connection.execute(
            "INSERT INTO web_requests (update_id, host, state) VALUES (?, ?, 'pending') "
            "ON CONFLICT (update_id, host) DO NOTHING",
            (update_id, host),
        )

    def pending(self, update_id: int | None = None) -> list[tuple[int, int, int, str]]:
        rows = self._database.connection.execute(
            "SELECT w.id, t.conversation_id, c.thread_id, w.host FROM web_requests w "
            "JOIN turns t ON t.update_id = w.update_id "
            "JOIN conversations c ON c.id = t.conversation_id "
            "WHERE w.state = 'pending' AND c.state = 'open' "
            "AND t.state IN ('done', 'interrupted') AND (? IS NULL OR w.update_id = ?) "
            "ORDER BY w.id",
            (update_id, update_id),
        ).fetchall()
        return [(r["id"], r["conversation_id"], r["thread_id"], r["host"]) for r in rows]

    def conversation_of(self, request_id: int) -> int | None:
        row = self._database.connection.execute(
            "SELECT t.conversation_id FROM web_requests w "
            "JOIN turns t ON t.update_id = w.update_id WHERE w.id = ?",
            (request_id,),
        ).fetchone()
        return None if row is None else row["conversation_id"]

    def bound(self, channel: str, chat: str, message_ref: str) -> str | None:
        row = self._database.connection.execute(
            "SELECT notice_key FROM messages WHERE channel = ? AND chat = ? "
            "AND platform_message_id = ? AND direction = 'out' AND delivery = 'sent'",
            (channel, chat, message_ref),
        ).fetchone()
        return None if row is None else row["notice_key"]

    def decide(self, request_id: int, update_id: int, *, allow: bool, now: datetime) -> int | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT w.host, w.state, w.decision_update_id, t.conversation_id "
                "FROM web_requests w JOIN turns t ON t.update_id = w.update_id "
                "JOIN conversations c ON c.id = t.conversation_id "
                "WHERE w.id = ? AND c.state = 'open'",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] != PENDING:
                return row["conversation_id"] if row["decision_update_id"] == update_id else None
            state = _ALLOWED if allow else _DENIED
            connection.execute(
                "UPDATE web_requests SET state = ?, decision_update_id = ? WHERE id = ?",
                (state, update_id, request_id),
            )
            if allow:
                connection.execute(
                    "INSERT INTO web_grants (host, granted_at) VALUES (?, ?) "
                    "ON CONFLICT (host) DO NOTHING",
                    (row["host"], stamp(now)),
                )
            connection.execute(
                "INSERT INTO turns (conversation_id, update_id, text, state) "
                "VALUES (?, ?, ?, 'pending')",
                (
                    row["conversation_id"],
                    update_id,
                    f"The Product Owner {state} web access to {row['host']}.",
                ),
            )
            settle_notice(
                connection, f"expert:web:{request_id}", _decision_message(row["host"], state)
            )
            connection.execute(
                "UPDATE conversations SET last_activity = ? WHERE id = ?",
                (stamp(now), row["conversation_id"]),
            )
            return row["conversation_id"]

    def settle_decided(self) -> None:
        """Settle every decided request again, so a decision left without its edit is made up."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT id, host, state FROM web_requests WHERE state IN (?, ?) ORDER BY id",
                (_ALLOWED, _DENIED),
            ).fetchall()
            for row in rows:
                settle_notice(
                    connection,
                    f"expert:web:{row['id']}",
                    _decision_message(row["host"], row["state"]),
                )
