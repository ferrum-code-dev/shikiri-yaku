"""The numbered migrations of the journal: migration N is MIGRATIONS[N - 1] and is never edited.

Migration 2 makes a notice unique per key and chat, not per key: the key names the notice and each
owner's chat gets its own copy of it (design §7).
Migration 3 keeps the time of the newest lifecycle event: the liveness rule needs to know whether
a clean stop happened before this run of the Operator (design §4).
Migration 4 keeps the note that came with a signal's classification: the Reasoner's suggestion or
its failure, which the message to the PO must carry (design §5).
Migration 5 keeps the body of an outbound message, so a restart can send what was recorded
(design §7, §10).
Migration 6 keeps the reason of a refused outbound message, the only thing a refusal leaves
behind (design §7).
Migration 7 keeps what the PO sent in the messages table, one row per Telegram update id, and the
offset the polling resumes from (design §7, §9). The body column holds an inbound item's text.
Migration 8 keeps the acknowledgement of a resolve op (design §10.1).
Migration 9 keeps the final body of a sent copy whose edit is journaled and not yet confirmed, so a
restart finishes the edit (design §7).
Migration 10 marks that edit confirmed without replacing the body: the body keeps the buttons the
copy was sent with, so a tap made before the edit is still recognized and answered (design §7).
Migration 11 claims each logical command reply in the same transaction as all of its parts.
Migration 12 stores Telegram topic threads on outbox rows and records the reconciled topic ids.
Migration 13 keeps the pull request of a review signal: its link and its commit count (design §4).
Migration 14 keeps a Reasoner call's usage and duration for a signal's verdict, as JSON (design §8).
Migration 15 keeps the typed host-tool proposal from the classification, as JSON (design §5).
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
    (
        "ALTER TABLE cursors ADD COLUMN lifecycle_at TEXT",
        """
        UPDATE cursors SET lifecycle_at = '1970-01-01T00:00:00+00:00'
        WHERE lifecycle_event_id IS NOT NULL
        """,
    ),
    ("ALTER TABLE signals ADD COLUMN classification_note TEXT",),
    ("ALTER TABLE messages ADD COLUMN body TEXT",),
    ("ALTER TABLE messages ADD COLUMN refusal TEXT",),
    (
        "ALTER TABLE messages ADD COLUMN update_id INTEGER",
        "ALTER TABLE messages ADD COLUMN kind TEXT",
        "ALTER TABLE messages ADD COLUMN sender INTEGER",
        "ALTER TABLE messages ADD COLUMN message_ref TEXT",
        "ALTER TABLE messages ADD COLUMN reply_to_ref TEXT",
        "ALTER TABLE messages ADD COLUMN data TEXT",
        "CREATE UNIQUE INDEX messages_update ON messages (update_id) WHERE update_id IS NOT NULL",
        "CREATE TABLE polling (channel TEXT PRIMARY KEY, next_offset INTEGER NOT NULL)",
    ),
    ("ALTER TABLE ops ADD COLUMN acknowledgement TEXT",),
    ("ALTER TABLE messages ADD COLUMN final_body TEXT",),
    ("ALTER TABLE messages ADD COLUMN final_edited INTEGER NOT NULL DEFAULT 0",),
    ("ALTER TABLE signals ADD COLUMN final_line TEXT",),
    ("CREATE TABLE command_replies (reply_key TEXT PRIMARY KEY)",),
    (
        "ALTER TABLE messages ADD COLUMN thread INTEGER",
        "DROP INDEX messages_notice",
        """
        CREATE UNIQUE INDEX messages_notice
            ON messages (notice_key, channel, chat, COALESCE(thread, -1))
        """,
        """
        CREATE TABLE topics (
            component TEXT,
            thread_id INTEGER NOT NULL,
            state TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX topics_component ON topics (COALESCE(component, ''))",
    ),
    (
        "ALTER TABLE signals ADD COLUMN pr_url TEXT",
        "ALTER TABLE signals ADD COLUMN pr_commits INTEGER",
    ),
    ("ALTER TABLE signals ADD COLUMN verdict_call TEXT",),
    ("ALTER TABLE signals ADD COLUMN install_proposal TEXT",),
)
