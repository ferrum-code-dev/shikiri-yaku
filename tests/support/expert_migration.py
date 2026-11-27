"""Remove later schema when reconstructing an older journal in migration tests."""

from __future__ import annotations

from typing import Protocol


class Connection(Protocol):
    def execute(self, sql: str, /) -> object: ...


def remove_event_identity(connection: Connection) -> None:
    """Put back the polled offset and take out the event keys, so migration 31 can run again."""
    connection.execute("DROP TABLE event_keys")
    connection.execute("ALTER TABLE messages DROP COLUMN event_key")
    connection.execute("ALTER TABLE polling DROP COLUMN cursor")
    connection.execute("ALTER TABLE polling DROP COLUMN last_id")
    connection.execute("ALTER TABLE polling ADD COLUMN next_offset INTEGER NOT NULL DEFAULT 0")


def remove_expert_schema(connection: Connection) -> None:
    remove_event_identity(connection)
    connection.execute("DROP INDEX topics_conversation")
    connection.execute("DROP INDEX topics_component")
    connection.execute("ALTER TABLE topics DROP COLUMN conversation_id")
    connection.execute("CREATE UNIQUE INDEX topics_component ON topics (COALESCE(component, ''))")
    connection.execute("ALTER TABLE messages DROP COLUMN conversation_id")
    connection.execute("DROP TABLE web_requests")
    connection.execute("DROP TABLE web_grants")
    connection.execute("DROP TABLE proposals")
    connection.execute("DROP TABLE turns")
    connection.execute("DROP TABLE conversations")
