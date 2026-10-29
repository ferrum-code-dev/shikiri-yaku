from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.adapters.telegram_topics import TOPIC_OPEN, TopicRecord

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database


class TopicStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, component: str | None) -> TopicRecord | None:
        row = self._database.connection.execute(
            "SELECT component, thread_id, state FROM topics WHERE component IS ?", (component,)
        ).fetchone()
        if row is None:
            return None
        return TopicRecord(row["component"], row["thread_id"], row["state"])

    def save(self, component: str | None, thread_id: int, state: str = TOPIC_OPEN) -> None:
        self._database.connection.execute(
            """
            INSERT INTO topics (component, thread_id, state) VALUES (?, ?, ?)
            ON CONFLICT (COALESCE(component, '')) DO UPDATE SET thread_id = excluded.thread_id,
                state = excluded.state
            """,
            (component, thread_id, state),
        )

    def all(self) -> list[TopicRecord]:
        rows = self._database.connection.execute(
            "SELECT component, thread_id, state FROM topics ORDER BY component"
        )
        return [TopicRecord(row["component"], row["thread_id"], row["state"]) for row in rows]

    def component_of(self, thread_id: int) -> str | None:
        row = self._database.connection.execute(
            "SELECT component FROM topics WHERE thread_id = ? AND state = ?",
            (thread_id, TOPIC_OPEN),
        ).fetchone()
        return None if row is None else row["component"]
