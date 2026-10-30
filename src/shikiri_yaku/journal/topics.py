from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database


@dataclass(frozen=True, slots=True)
class TopicRecord:
    """A row of the topics table. The Channel adapter decides what each state means."""

    component: str | None
    thread_id: int
    state: str


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

    def save(self, component: str | None, thread_id: int, state: str) -> None:
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

    def by_thread(self, thread_id: int) -> TopicRecord | None:
        # keep: a closed topic keeps its thread, so the caller reads the state to tell if it is live
        row = self._database.connection.execute(
            "SELECT component, thread_id, state FROM topics WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return None
        return TopicRecord(row["component"], row["thread_id"], row["state"])
