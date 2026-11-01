from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.loop_guard import Cause
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.journal.database import Inserted, parse_stamp, stamp
from shikiri_yaku.ports import Ack, InstallOp

if TYPE_CHECKING:
    import sqlite3

    from shikiri_yaku.domain.loop_guard import AutoOp
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database


class OpState(StrEnum):
    INTENDED = "intended"
    APPROVED = "approved"
    ISSUED = "issued"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    DECLINED = "declined"
    EXPIRED = "expired"
    VOID = "void"
    UNKNOWN = "unknown"


INSTALL_KIND: Final = "install"


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
    acknowledgement: Ack | None
    updated_at: datetime


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
        acknowledgement=None
        if row["acknowledgement"] is None
        else Ack.model_validate_json(row["acknowledgement"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
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

    def get_install(self, identity: str) -> InstallOp | None:
        row = self._database.connection.execute(
            "SELECT * FROM ops WHERE identity = ? AND kind = ?", (identity, INSTALL_KIND)
        ).fetchone()
        return None if row is None else _install_op(row)

    def install_op(self, op_id: int) -> InstallOp | None:
        row = self._database.connection.execute(
            "SELECT * FROM ops WHERE id = ? AND kind = ?", (op_id, INSTALL_KIND)
        ).fetchone()
        return None if row is None else _install_op(row)

    def intended_installs(self) -> list[InstallOp]:
        rows = self._database.connection.execute(
            "SELECT * FROM ops WHERE kind = ? AND state = ? ORDER BY id",
            (INSTALL_KIND, OpState.INTENDED),
        )
        return [_install_op(row) for row in rows]

    def intend_install(
        self,
        identity: str,
        signal: Signal,
        payload: InstallOpPayload,
        now: datetime,
    ) -> InstallOp:
        """Persist the validated plan once and return the stored version, never a re-derived one."""
        self.intend(
            NewOp(
                identity,
                INSTALL_KIND,
                signal.project,
                signal.task_id,
                signal.reason,
                payload.model_dump_json(),
            ),
            now,
        )
        stored = self.get_install(identity)
        if stored is None:
            message = f"op identity '{identity}' belongs to a different operation"
            raise ValueError(message)
        return stored

    def approve_install(self, op_id: int, number: int, now: datetime) -> bool:
        with self._database.transaction():
            operation = self.install_op(op_id)
            if (
                operation is None
                or operation.payload.number != number
                or operation.state != OpState.INTENDED
            ):
                return False
            changed = self._database.connection.execute(
                """
                UPDATE ops SET state = ?, approved_at = COALESCE(approved_at, ?), updated_at = ?
                WHERE id = ? AND kind = ? AND state = ?
                """,
                (
                    OpState.APPROVED,
                    stamp(now),
                    stamp(now),
                    op_id,
                    INSTALL_KIND,
                    OpState.INTENDED,
                ),
            ).rowcount
            return changed == 1

    def expire_install(self, op_id: int, number: int, now: datetime) -> bool:
        return self._transition_install(op_id, number, OpState.EXPIRED, now)

    def void_install(self, op_id: int, number: int, now: datetime) -> bool:
        return self._transition_install(op_id, number, OpState.VOID, now)

    def void_issued(self, op_id: int, now: datetime) -> bool:
        """Void an issued install once a new approval has been queued in its place."""
        return self.transition(op_id, OpState.ISSUED, OpState.VOID, now)

    def decline_install(self, op_id: int, number: int, now: datetime) -> bool:
        with self._database.transaction():
            operation = self.install_op(op_id)
            if operation is None or operation.payload.number != number:
                return False
            if operation.state == OpState.DECLINED:
                return True
            if operation.state != OpState.INTENDED:
                return False
            return self.transition(op_id, OpState.INTENDED, OpState.DECLINED, now)

    def _transition_install(self, op_id: int, number: int, new: OpState, now: datetime) -> bool:
        with self._database.transaction():
            operation = self.install_op(op_id)
            if (
                operation is None
                or operation.payload.number != number
                or operation.state != OpState.INTENDED
            ):
                return False
            return self.transition(op_id, OpState.INTENDED, new, now)

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

    def acknowledge_resolve(self, op_id: int, ack: Ack, now: datetime) -> None:
        """Save the acknowledgement and command id of an issued resolve; the op stays issued."""
        self._database.connection.execute(
            """
            UPDATE ops SET acknowledgement = ?, command_id = ?, updated_at = ?
            WHERE id = ? AND kind = 'resolve' AND state = ? AND acknowledgement IS NULL
            """,
            (ack.model_dump_json(), str(ack.id), stamp(now), op_id, OpState.ISSUED),
        )

    def acknowledge_install(
        self, op_id: int, ack: Ack, output_path: str | None, now: datetime
    ) -> bool:
        """Save the acknowledgement, confirming it if its event arrived first."""
        matched = self._database.connection.execute(
            """
            UPDATE ops SET state = CASE WHEN resolved_command_id = ? THEN ? ELSE state END,
                acknowledgement = ?, command_id = ?,
                output_path = COALESCE(?, output_path), updated_at = ?
            WHERE id = ? AND kind = ? AND state = ?
            """,
            (
                str(ack.id),
                OpState.CONFIRMED,
                ack.model_dump_json(),
                str(ack.id),
                output_path,
                stamp(now),
                op_id,
                INSTALL_KIND,
                OpState.ISSUED,
            ),
        ).rowcount
        return matched == 1 and self.install_resolution_seen(op_id, str(ack.id))

    def observe_install_resolution(
        self, project: str, escalation_id: str, command_id: str | None
    ) -> None:
        """Keep an event id while its issued install is still waiting for the acknowledgement."""
        if command_id is None:
            return
        rows = self._database.connection.execute(
            "SELECT id, payload FROM ops WHERE kind = ? AND project = ? AND state = ?",
            (INSTALL_KIND, project, OpState.ISSUED),
        ).fetchall()
        for row in rows:
            payload = InstallOpPayload.model_validate_json(row["payload"])
            if payload.escalation_id != escalation_id:
                continue
            self._database.connection.execute(
                """
                UPDATE ops SET resolved_command_id = COALESCE(resolved_command_id, ?)
                WHERE id = ? AND state = ?
                """,
                (command_id, row["id"], OpState.ISSUED),
            )

    def install_resolution_seen(self, op_id: int, command_id: str) -> bool:
        row = self._database.connection.execute(
            "SELECT resolved_command_id FROM ops WHERE id = ? AND kind = ?",
            (op_id, INSTALL_KIND),
        ).fetchone()
        return row is not None and row["resolved_command_id"] == command_id

    def mark_install_check_failed(self, op_id: int, now: datetime) -> None:
        """Record the first check that did not find its tool, without counting later retries."""
        self._database.connection.execute(
            """
            UPDATE ops SET check_failed_at = COALESCE(check_failed_at, ?)
            WHERE id = ? AND kind = ? AND state = ?
            """,
            (stamp(now), op_id, INSTALL_KIND, OpState.ISSUED),
        )

    def confirm_command(self, project: str, command_id: str, now: datetime) -> bool:
        """Confirm the issued resolve or install that Shikiru's event names.

        An unknown command id changes nothing.
        """
        confirmed = self._database.connection.execute(
            """
            UPDATE ops SET state = ?, updated_at = ?
            WHERE kind IN ('resolve', ?) AND project = ? AND command_id = ? AND state = ?
            """,
            (OpState.CONFIRMED, stamp(now), INSTALL_KIND, project, command_id, OpState.ISSUED),
        ).rowcount
        return confirmed == 1

    def carries_command(self, project: str, command_id: str) -> bool:
        """Say whether a resolve or install op, in any state, carries this command id."""
        row = self._database.connection.execute(
            "SELECT 1 FROM ops WHERE kind IN ('resolve', ?) AND project = ? AND command_id = ?",
            (INSTALL_KIND, project, command_id),
        ).fetchone()
        return row is not None

    def non_final(self) -> list[StoredOp]:
        """Return ops whose effects still need observation or a decision."""
        rows = self._database.connection.execute(
            "SELECT * FROM ops WHERE state NOT IN (?, ?, ?, ?, ?) ORDER BY id",
            (
                OpState.CONFIRMED,
                OpState.FAILED,
                OpState.DECLINED,
                OpState.EXPIRED,
                OpState.VOID,
            ),
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


def _install_op(row: sqlite3.Row) -> InstallOp:
    payload = InstallOpPayload.model_validate_json(row["payload"])
    created_at = parse_stamp(row["created_at"])
    if created_at is None:
        message = f"install op {row['id']} has no creation time"
        raise ValueError(message)
    return InstallOp(
        id=row["id"],
        identity=row["identity"],
        project=row["project"],
        task_id=row["task_id"],
        reason=row["reason"],
        payload=payload,
        state=row["state"],
        created_at=created_at,
    )


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
