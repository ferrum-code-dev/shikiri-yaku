"""The numbered migrations of the journal: migration N is MIGRATIONS[N - 1] and is never edited.

Migration 2 makes a notice unique per key and chat, not per key: the key names the notice and each
owner's chat gets its own copy of it (design §7).
"""

from __future__ import annotations

from typing import Final

MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        """
        CREATE TABLE cursors (
            project TEXT PRIMARY KEY,
            last_event_id INTEGER NOT NULL,
            lifecycle_event_id INTEGER,
            lifecycle_kind TEXT,
            lifecycle_has_reason INTEGER,
            respected_stop_id INTEGER
        )
        """,
        """
        CREATE TABLE signals (
            number INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            project TEXT NOT NULL,
            task_id TEXT,
            escalation_id TEXT,
            kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            text TEXT NOT NULL,
            category TEXT,
            classified_by TEXT,
            verdict TEXT,
            classified_at TEXT,
            handler TEXT,
            state TEXT NOT NULL,
            notes TEXT,
            wake_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX signals_parked ON signals (wake_at) WHERE state = 'parked'",
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_number INTEGER REFERENCES signals (number),
            direction TEXT NOT NULL,
            channel TEXT NOT NULL,
            chat TEXT NOT NULL,
            platform_message_id TEXT,
            purpose TEXT,
            notice_key TEXT UNIQUE,
            delivery TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX messages_platform
            ON messages (channel, chat, platform_message_id, direction)
            WHERE platform_message_id IS NOT NULL
        """,
        """
        CREATE TABLE ops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            project TEXT NOT NULL,
            task_id TEXT,
            reason TEXT,
            payload TEXT,
            state TEXT NOT NULL,
            command_id TEXT,
            output_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
    (
        """
        CREATE TABLE messages_by_destination (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_number INTEGER REFERENCES signals (number),
            direction TEXT NOT NULL,
            channel TEXT NOT NULL,
            chat TEXT NOT NULL,
            platform_message_id TEXT,
            purpose TEXT,
            notice_key TEXT,
            delivery TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "INSERT INTO messages_by_destination SELECT * FROM messages",
        "DROP TABLE messages",
        "ALTER TABLE messages_by_destination RENAME TO messages",
        """
        CREATE UNIQUE INDEX messages_platform
            ON messages (channel, chat, platform_message_id, direction)
            WHERE platform_message_id IS NOT NULL
        """,
        "CREATE UNIQUE INDEX messages_notice ON messages (notice_key, channel, chat)",
    ),
)
