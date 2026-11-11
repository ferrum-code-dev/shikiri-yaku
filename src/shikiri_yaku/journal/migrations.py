"""The numbered migrations of the journal: migration N is MIGRATIONS[N - 1] and is never edited.

Migration 2 makes a notice unique per key and chat, not per key: the key names the notice and each
owner's chat gets its own copy of it.
Migration 3 keeps the time of the newest lifecycle event: the liveness rule needs to know whether
a clean stop happened before this run of the Operator.
Migration 4 keeps the note that came with a signal's classification: the Reasoner's suggestion or
its failure, which the message to the PO must carry.
Migration 5 keeps the body of an outbound message, so a restart can send what was recorded.
Migration 6 keeps the reason of a refused outbound message, the only thing a refusal leaves
behind.
Migration 7 keeps what the PO sent in the messages table, one row per channel update id, and the
offset the polling resumes from. The body column holds an inbound item's text.
Migration 8 keeps the acknowledgement of a resolve op.
Migration 9 keeps the final body of a sent copy whose edit is journaled and not yet confirmed, so a
restart finishes the edit.
Migration 10 marks that edit confirmed without replacing the body: the body keeps the buttons the
copy was sent with, so a tap made before the edit is still recognized and answered.
Migration 11 claims each logical command reply in the same transaction as all of its parts.
Migration 12 stores channel topic threads on outbox rows and records the reconciled topic ids.
Migration 13 keeps the pull request of a review signal: its link and its commit count.
Migration 14 keeps a Reasoner call's usage and duration for a signal's verdict, as JSON.
Migration 15 keeps the typed host-tool proposal from the classification, as JSON.
Migration 16 records install outcomes and a resolution event that arrived before its
acknowledgement.
Migration 17 records a rejected recovery answer before retryable PO notification work begins.
Migration 20 keeps the slot of an outbound message, so a newer copy in the same slot replaces it.
Migration 21 gives each copy sent before slots the slot its kind has now, read from its notice key,
so an upgraded journal replaces the approval it already holds.
Migration 24 keeps the name of each one-time repair that ran, so an upgraded journal repairs once.
Migration 26 keeps the effort a conversation was given with `/effort`, which its next turn passes
to the agent.
Migration 27 keeps the state of each Expert task proposal, so its buttons act at most once.
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
    (
        "ALTER TABLE ops ADD COLUMN approved_at TEXT",
        "ALTER TABLE ops ADD COLUMN check_failed_at TEXT",
        "ALTER TABLE ops ADD COLUMN resolved_command_id TEXT",
    ),
    ("ALTER TABLE ops ADD COLUMN install_rejection TEXT",),
    ("ALTER TABLE ops ADD COLUMN failure_reason TEXT",),
    ("ALTER TABLE messages ADD COLUMN slot TEXT",),
    (
        """
        UPDATE messages SET slot = 'question'
        WHERE purpose = 'question' AND (
            notice_key LIKE 'forward:%' OR notice_key LIKE 'reshow:%' OR notice_key LIKE 'open:%')
        """,
        """
        UPDATE messages SET slot = 'install'
        WHERE purpose = 'approval' AND notice_key LIKE 'install:%'
        """,
        """
        UPDATE messages
        SET slot = 'confirm:' || substr(notice_key, 9, instr(substr(notice_key, 9) || ':', ':') - 1)
        WHERE purpose = 'approval' AND notice_key LIKE 'confirm:%'
        """,
    ),
    ("ALTER TABLE messages ADD COLUMN final_line TEXT",),
    ("ALTER TABLE messages ADD COLUMN standing INTEGER NOT NULL DEFAULT 0",),
    ("CREATE TABLE repairs (name TEXT PRIMARY KEY)",),
    (
        "ALTER TABLE topics ADD COLUMN conversation_id INTEGER REFERENCES conversations (id)",
        "ALTER TABLE messages ADD COLUMN conversation_id INTEGER REFERENCES conversations (id)",
        "DROP INDEX topics_component",
        (
            "CREATE UNIQUE INDEX topics_component ON topics (COALESCE(component, '')) "
            "WHERE conversation_id IS NULL"
        ),
        (
            "CREATE UNIQUE INDEX topics_conversation ON topics (conversation_id) "
            "WHERE conversation_id IS NOT NULL"
        ),
        """CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, update_id INTEGER NOT NULL UNIQUE,
            chat INTEGER NOT NULL, origin_thread INTEGER, scope TEXT NOT NULL,
            state TEXT NOT NULL, thread_id INTEGER, session TEXT, last_activity TEXT NOT NULL
        )""",
        """CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            update_id INTEGER NOT NULL UNIQUE, text TEXT NOT NULL, state TEXT NOT NULL,
            answer TEXT, call TEXT
        )""",
    ),
    ("ALTER TABLE conversations ADD COLUMN effort TEXT",),
    (
        """CREATE TABLE proposals (
            update_id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations (id),
            state TEXT NOT NULL
        )""",
    ),
)
