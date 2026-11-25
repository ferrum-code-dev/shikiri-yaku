"""Remove Expert schema when reconstructing an older journal in migration tests."""

from __future__ import annotations

from typing import Protocol


class Connection(Protocol):
    def execute(self, sql: str, /) -> object: ...


def remove_expert_schema(connection: Connection) -> None:
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
