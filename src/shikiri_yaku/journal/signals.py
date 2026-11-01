from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from shikiri_yaku.domain.classification import InstallProposal
from shikiri_yaku.domain.signal import Kind, PullRequest, Signal
from shikiri_yaku.journal.database import Inserted, stamp

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection
    from datetime import datetime

    from shikiri_yaku.domain.classification import CallRecord
    from shikiri_yaku.domain.signal import Closing
    from shikiri_yaku.journal.database import Database


class SignalState(StrEnum):
    NEW = "new"
    HANDLING = "handling"
    PARKED = "parked"
    FORWARDED = "forwarded"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass(frozen=True)
class NewSignal:
    id: str
    project: str
    kind: str
    reason: str
    text: str
    task_id: str | None = None
    escalation_id: str | None = None
    pull_request: PullRequest | None = None


type Change = NewSignal | Closing


@dataclass(frozen=True)
class StoredSignal:
    number: int
    id: str
    project: str
    kind: str
    reason: str
    text: str
    task_id: str | None
    escalation_id: str | None
    category: str | None
    classified_by: str | None
    verdict: str | None
    classification_note: str | None
    handler: str | None
    state: SignalState
    notes: str | None
    wake_at: str | None
    pull_request: PullRequest | None = None

    def to_signal(self) -> Signal:
        return Signal(
            project=self.project,
            kind=Kind(self.kind),
            source_id=self.id.removeprefix(f"{self.project}:{self.kind}:"),
            reason=self.reason,
            text=self.text,
            task_id=self.task_id,
            escalation_id=self.escalation_id,
            pull_request=self.pull_request,
        )


def _signal(row: sqlite3.Row) -> StoredSignal:
    return StoredSignal(
        number=row["number"],
        id=row["id"],
        project=row["project"],
        kind=row["kind"],
        reason=row["reason"],
        text=row["text"],
        task_id=row["task_id"],
        escalation_id=row["escalation_id"],
        category=row["category"],
        classified_by=row["classified_by"],
        verdict=row["verdict"],
        classification_note=row["classification_note"],
        handler=row["handler"],
        state=SignalState(row["state"]),
        notes=row["notes"],
        wake_at=row["wake_at"],
        pull_request=None
        if row["pr_url"] is None
        else PullRequest(row["pr_url"], row["pr_commits"]),
    )


class SignalStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def insert_if_absent(self, signal: NewSignal, now: datetime) -> Inserted:
        """Insert a signal unless its id is known; the signal number is the row id."""
        pull = signal.pull_request
        with self._database.transaction() as connection:
            created = connection.execute(
                """
                INSERT INTO signals (id, project, task_id, escalation_id, kind, reason, text,
                                     state, created_at, updated_at, pr_url, pr_commits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    signal.id,
                    signal.project,
                    signal.task_id,
                    signal.escalation_id,
                    signal.kind,
                    signal.reason,
                    signal.text,
                    SignalState.NEW,
                    stamp(now),
                    stamp(now),
                    None if pull is None else pull.url,
                    None if pull is None else pull.commits,
                ),
            ).rowcount
            row = connection.execute("SELECT number FROM signals WHERE id = ?", (signal.id,))
            return Inserted(row.fetchone()["number"], is_new=created == 1)

    def close(self, closing: Closing, now: datetime) -> int:
        """Close the non-final signals that match every field given; return how many."""
        return self._database.connection.execute(
            """
            UPDATE signals SET state = :closed, updated_at = :now,
                final_line = COALESCE(final_line, :final_line)
            WHERE project = :project AND state NOT IN (:resolved, :closed)
                AND (:forwarded_only = 0 OR state = :forwarded)
                AND (:kind IS NULL OR kind = :kind)
                AND (:escalation_id IS NULL OR escalation_id = :escalation_id)
                AND (:task_id IS NULL OR task_id = :task_id)
                AND (:reason IS NULL OR reason = :reason)
            """,
            {
                "closed": SignalState.CLOSED,
                "resolved": SignalState.RESOLVED,
                "forwarded": SignalState.FORWARDED,
                "forwarded_only": closing.forwarded_only,
                "now": stamp(now),
                "project": closing.project,
                "kind": closing.kind,
                "escalation_id": closing.escalation_id,
                "task_id": closing.task_id,
                "reason": closing.reason,
                "final_line": closing.final_line,
            },
        ).rowcount

    def set_final_line(
        self, number: int, final_line: str, now: datetime, *, replace: bool = False
    ) -> None:
        """Record the line its copies end with, replacing a matched early event note."""
        self._database.connection.execute(
            """
            UPDATE signals SET final_line = CASE WHEN ? THEN ? ELSE COALESCE(final_line, ?) END,
                updated_at = ?
            WHERE number = ?
            """,
            (replace, final_line, final_line, stamp(now), number),
        )

    def close_removed(self, configured: Collection[str], note: str, now: datetime) -> int:
        """Close open signals of projects not in `configured`, with the note; return the count."""
        closed = 0
        with self._database.transaction() as connection:
            projects = connection.execute(
                "SELECT DISTINCT project FROM signals WHERE state NOT IN (?, ?)",
                (SignalState.RESOLVED, SignalState.CLOSED),
            ).fetchall()
            for row in projects:
                if row["project"] not in configured:
                    closed += connection.execute(
                        """
                        UPDATE signals SET state = :closed, updated_at = :now,
                            notes = CASE WHEN notes IS NULL THEN :note
                                         ELSE notes || char(10) || :note END
                        WHERE project = :project AND state NOT IN (:resolved, :closed)
                        """,
                        {
                            "closed": SignalState.CLOSED,
                            "resolved": SignalState.RESOLVED,
                            "now": stamp(now),
                            "note": note,
                            "project": row["project"],
                        },
                    ).rowcount
        return closed

    def has_open(self, project: str, kind: str, reason: str | None = None) -> bool:
        """Say whether the project has a non-final signal of the kind (and reason, if given)."""
        row = self._database.connection.execute(
            """
            SELECT 1 FROM signals WHERE project = ? AND kind = ? AND state NOT IN (?, ?)
                AND (? IS NULL OR reason = ?)
            """,
            (project, kind, SignalState.RESOLVED, SignalState.CLOSED, reason, reason),
        ).fetchone()
        return row is not None

    def get(self, number: int) -> StoredSignal | None:
        row = self._database.connection.execute(
            "SELECT * FROM signals WHERE number = ?", (number,)
        ).fetchone()
        return None if row is None else _signal(row)

    def by_id(self, signal_id: str) -> StoredSignal | None:
        row = self._database.connection.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return None if row is None else _signal(row)

    def number_of(self, signal: Signal) -> int | None:
        stored = self.by_id(signal.id)
        return None if stored is None else stored.number

    def classify(
        self,
        number: int,
        *,
        category: str,
        classified_by: str,
        verdict: str | None,
        handler: str | None,
        now: datetime,
        note: str = "",
        call: CallRecord | None = None,
        install_proposal: InstallProposal | None = None,
    ) -> None:
        verdict_call = None if call is None else json.dumps(asdict(call), sort_keys=True)
        proposal = None if install_proposal is None else install_proposal.model_dump_json()
        self._database.connection.execute(
            """
            UPDATE signals SET category = ?, classified_by = ?, verdict = ?,
                               classification_note = ?, classified_at = ?, handler = ?,
                               verdict_call = ?, install_proposal = ?, updated_at = ?
            WHERE number = ?
            """,
            (
                category,
                classified_by,
                verdict,
                note,
                stamp(now),
                handler,
                verdict_call,
                proposal,
                stamp(now),
                number,
            ),
        )

    def install_proposal(self, signal_id: str) -> InstallProposal | None:
        """Read the typed host-tool proposal stored with a signal's classification."""
        row = self._database.connection.execute(
            "SELECT install_proposal FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return (
            None
            if row is None or row["install_proposal"] is None
            else InstallProposal.model_validate_json(row["install_proposal"])
        )

    def transition(
        self,
        number: int,
        expected: SignalState,
        new: SignalState,
        now: datetime,
        *,
        wake_at: datetime | None = None,
        note: str | None = None,
        final_line: str | None = None,
    ) -> bool:
        """Move a signal only if it is still in `expected`; False means someone else moved it.

        `final_line` is recorded once, for the copies that end with it.
        """
        moved = self._database.connection.execute(
            """
            UPDATE signals SET state = :new, wake_at = :wake_at, updated_at = :now,
                notes = CASE WHEN :note IS NULL THEN notes
                             WHEN notes IS NULL THEN :note
                             ELSE notes || char(10) || :note END,
                final_line = COALESCE(final_line, :final_line)
            WHERE number = :number AND state = :expected
            """,
            {
                "new": new,
                "wake_at": None if wake_at is None else stamp(wake_at),
                "now": stamp(now),
                "note": note,
                "final_line": final_line,
                "number": number,
                "expected": expected,
            },
        ).rowcount
        return moved == 1

    def parked_due(self, now: datetime) -> list[StoredSignal]:
        rows = self._database.connection.execute(
            "SELECT * FROM signals WHERE state = ? AND wake_at <= ? ORDER BY wake_at, number",
            (SignalState.PARKED, stamp(now)),
        )
        return [_signal(row) for row in rows]

    def forwarded_escalations(self) -> list[StoredSignal]:
        """Return the forwarded signals that have an escalation, oldest first."""
        rows = self._database.connection.execute(
            "SELECT * FROM signals WHERE state = ? AND escalation_id IS NOT NULL ORDER BY number",
            (SignalState.FORWARDED,),
        )
        return [_signal(row) for row in rows]

    def new(self) -> list[StoredSignal]:
        """Return the signals waiting for the Router, oldest first."""
        rows = self._database.connection.execute(
            "SELECT * FROM signals WHERE state = ? ORDER BY number", (SignalState.NEW,)
        )
        return [_signal(row) for row in rows]

    def requeue_handling(self, now: datetime) -> int:
        """Put every signal left in `handling` back to `new`; return how many."""
        return self._database.connection.execute(
            "UPDATE signals SET state = ?, updated_at = ? WHERE state = ?",
            (SignalState.NEW, stamp(now), SignalState.HANDLING),
        ).rowcount
