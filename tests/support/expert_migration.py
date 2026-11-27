"""Remove later schema when reconstructing an older journal in migration tests."""

from __future__ import annotations

from typing import Final, Protocol


class Connection(Protocol):
    def execute(self, sql: str, /) -> object: ...


_CHAT: Final = (
    "CASE instr(place, '/') WHEN 0 THEN place ELSE substr(place, 1, instr(place, '/') - 1) END"
)
_THREAD: Final = (
    "CASE instr(place, '/') WHEN 0 THEN NULL ELSE substr(place, instr(place, '/') + 1) END"
)


def remove_places(connection: Connection) -> None:
    """Put back the chat and thread of each message, so migration 32 can run again."""
    connection.execute("DROP INDEX messages_platform")
    connection.execute("DROP INDEX messages_notice")
    connection.execute("ALTER TABLE messages ADD COLUMN chat TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE messages ADD COLUMN thread INTEGER")
    connection.execute(f"UPDATE messages SET chat = {_CHAT}, thread = {_THREAD}")  # noqa: S608
    connection.execute("ALTER TABLE messages DROP COLUMN place")
    connection.execute(
        "CREATE UNIQUE INDEX messages_platform ON messages "
        "(channel, chat, platform_message_id, direction) WHERE platform_message_id IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX messages_notice ON messages "
        "(notice_key, channel, chat, COALESCE(thread, -1))"
    )


def remove_event_identity(connection: Connection) -> None:
    """Put back the polled offset and take out the event keys, so migration 31 can run again."""
    remove_places(connection)
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
