"""Read-only queries behind `stats` (design §15): the only SQL that reports on the journal."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.journal.database import parse_stamp
from shikiri_yaku.journal.messages import Direction
from shikiri_yaku.journal.signals import SignalState
from shikiri_yaku.ports import CompletedEscalation

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from shikiri_yaku.journal.database import Database

_INSTALL: Final = "install"
_PARAMETERS: Final = {
    "out": Direction.OUT,
    "question": Purpose.QUESTION,
    "approval": Purpose.APPROVAL,
    "escalation": Kind.ESCALATION,
    "resolved": SignalState.RESOLVED,
    "closed": SignalState.CLOSED,
    "install": _INSTALL,
}


class JournalStats:
    """The journal's figures for `stats`. It only reads: a seeded journal gives the same figures."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def completed_escalations(self) -> list[CompletedEscalation]:
        # keep: one row per signal, so a question sent to several chats or re-shown counts once
        rows = self._rows(
            """
            SELECT s.created_at, s.updated_at, (
                SELECT MIN(m.created_at) FROM messages m WHERE m.signal_number = s.number
                AND m.direction = :out AND m.purpose IN (:question, :approval)
            ) AS queued_at
            FROM signals s
            WHERE s.kind = :escalation AND s.state IN (:resolved, :closed)
            ORDER BY s.number
            """
        )
        return [
            CompletedEscalation(
                created_at=datetime.fromisoformat(row["created_at"]),
                final_at=datetime.fromisoformat(row["updated_at"]),
                first_queued_at=parse_stamp(row["queued_at"]),
            )
            for row in rows
        ]

    def handled_without_po(self) -> Mapping[str | None, int]:
        return _counts(
            self._rows(
                """
                SELECT s.category AS key FROM signals s
                WHERE s.state = :resolved AND NOT EXISTS (
                    SELECT 1 FROM messages m WHERE m.signal_number = s.number AND m.direction = :out
                    AND m.purpose IN (:question, :approval))
                ORDER BY key
                """
            )
        )

    def escalation_reasons(self) -> Mapping[str, int]:
        return _counts(
            self._rows("SELECT reason AS key FROM signals WHERE kind = :escalation ORDER BY key")
        )

    def escalation_verdicts(self) -> Mapping[str | None, int]:
        return _counts(
            self._rows("SELECT verdict AS key FROM signals WHERE kind = :escalation ORDER BY key")
        )

    def install_states(self) -> Mapping[str, int]:
        return _counts(
            self._rows("SELECT state AS key FROM ops WHERE kind = :install ORDER BY key")
        )

    def _rows(self, sql: str) -> list[sqlite3.Row]:
        return self._database.connection.execute(sql, _PARAMETERS).fetchall()


def _counts(rows: list[sqlite3.Row]) -> Mapping[Any, int]:
    """Count the rows by key, in the order the query sorted the keys."""
    return Counter(row["key"] for row in rows)
