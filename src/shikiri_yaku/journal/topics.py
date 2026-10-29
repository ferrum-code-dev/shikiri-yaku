from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database

GENERAL_TOPIC: Final = "general"
TOPIC_OPEN: Final = "open"
TOPIC_CLOSED: Final = "closed"


@dataclass(frozen=True, slots=True)
class StoredTopic:
    component: str | None
    thread_id: int
    state: str


class TopicStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, component: str | None) -> StoredTopic | None:
        row = self._database.connection.execute(
            "SELECT component, thread_id, state FROM topics WHERE component IS ?", (component,)
        ).fetchone()
        if row is None:
            return None
        return StoredTopic(row["component"], row["thread_id"], row["state"])

    def save(self, component: str | None, thread_id: int, state: str = TOPIC_OPEN) -> None:
        self._database.connection.execute(
            """
            INSERT INTO topics (component, thread_id, state) VALUES (?, ?, ?)
            ON CONFLICT (COALESCE(component, '')) DO UPDATE SET thread_id = excluded.thread_id,
                state = excluded.state
            """,
            (component, thread_id, state),
        )

    def all(self) -> list[StoredTopic]:
        rows = self._database.connection.execute(
            "SELECT component, thread_id, state FROM topics ORDER BY component"
        )
        return [StoredTopic(row["component"], row["thread_id"], row["state"]) for row in rows]
