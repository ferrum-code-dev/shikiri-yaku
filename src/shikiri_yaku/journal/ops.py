from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from shikiri_yaku.domain.loop_guard import Cause
from shikiri_yaku.journal.database import Inserted, parse_stamp, stamp

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from shikiri_yaku.domain.loop_guard import AutoOp
    from shikiri_yaku.journal.database import Database


class OpState(StrEnum):
    INTENDED = "intended"
    APPROVED = "approved"
    ISSUED = "issued"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NewOp:
    """An effect about to happen; `payload` is JSON text, for an install the argument list."""

    identity: str
    kind: str
    project: str
    task_id: str | None = None
    reason: str | None = None
    payload: str | None = None
    state: OpState = OpState.INTENDED


@dataclass(frozen=True)
class StoredOp:
    id: int
    identity: str
    kind: str
    project: str
    task_id: str | None
    reason: str | None
    payload: str | None
    state: OpState
    command_id: str | None
    output_path: str | None


def _op(row: sqlite3.Row) -> StoredOp:
    return StoredOp(
        id=row["id"],
        identity=row["identity"],
        kind=row["kind"],
        project=row["project"],
        task_id=row["task_id"],
        reason=row["reason"],
        payload=row["payload"],
        state=OpState(row["state"]),
        command_id=row["command_id"],
        output_path=row["output_path"],
    )


class OpStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def intend(self, op: NewOp, now: datetime) -> Inserted:
        """Journal an effect before it happens; the same identity twice is one op."""
        with self._database.transaction() as connection:
            created = connection.execute(
                """
                INSERT INTO ops (identity, kind, project, task_id, reason, payload, state,
                                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (identity) DO NOTHING
                """,
                (
                    op.identity,
                    op.kind,
                    op.project,
                    op.task_id,
                    op.reason,
                    op.payload,
                    op.state,
                    stamp(now),
                    stamp(now),
                ),
            ).rowcount
            row = connection.execute("SELECT id FROM ops WHERE identity = ?", (op.identity,))
            return Inserted(row.fetchone()["id"], is_new=created == 1)

    def get(self, op_id: int) -> StoredOp | None:
        row = self._database.connection.execute(
            "SELECT * FROM ops WHERE id = ?", (op_id,)
        ).fetchone()
        return None if row is None else _op(row)

    def transition(
        self,
        op_id: int,
        expected: OpState,
        new: OpState,
        now: datetime,
        *,
        command_id: str | None = None,
        output_path: str | None = None,
    ) -> bool:
        """Move an op only if it is still in `expected`; exactly one concurrent caller wins."""
        moved = self._database.connection.execute(
            """
            UPDATE ops SET state = :new, updated_at = :now,
                command_id = COALESCE(:command_id, command_id),
                output_path = COALESCE(:output_path, output_path)
            WHERE id = :id AND state = :expected
            """,
            {
                "new": new,
                "now": stamp(now),
                "command_id": command_id,
                "output_path": output_path,
                "id": op_id,
                "expected": expected,
            },
        ).rowcount
        return moved == 1

    def non_final(self) -> list[StoredOp]:
        """Ops to reconcile at start: everything but `confirmed` and `failed`."""
        rows = self._database.connection.execute(
            "SELECT * FROM ops WHERE state NOT IN (?, ?) ORDER BY id",
            (OpState.CONFIRMED, OpState.FAILED),
        )
        return [_op(row) for row in rows]

    def created_times(
        self, kind: str, project: str, cause: Cause | None, since: datetime | None
    ) -> list[datetime]:
        """Return when ops of the kind were journaled after `since`, oldest first.

        With a cause only that task and reason count (a missing one matches a missing one);
        without it the whole project does.
        """
        query = "SELECT created_at FROM ops WHERE kind = ? AND project = ?"
        arguments: list[str | None] = [kind, project]
        if cause is not None:
            query += " AND task_id IS ? AND reason IS ?"
            arguments += [cause.task_id, cause.reason]
        if since is not None:
            query += " AND created_at > ?"
            arguments.append(stamp(since))
        rows = self._database.connection.execute(query + " ORDER BY id", arguments)
        return [moment for row in rows if (moment := parse_stamp(row["created_at"]))]


class JournalActions:
    """The ops table as the loop guard sees it."""

    def __init__(self, ops: OpStore) -> None:
        self._ops = ops

    def times(
        self, kind: str, project: str, cause: Cause | None, since: datetime | None
    ) -> list[datetime]:
        return self._ops.created_times(kind, project, cause, since)

    def record(self, op: AutoOp, now: datetime) -> int:
        cause = op.cause or Cause(None, None)
        state = OpState.FAILED if op.refused else OpState.INTENDED
        new = NewOp(
            op.identity, op.kind, op.project, cause.task_id, cause.reason, op.payload, state
        )
        return self._ops.intend(new, now).row_id
