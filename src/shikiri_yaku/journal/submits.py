"""Submit ops: a goal is journaled before Shikiru is asked to take it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.database import JournalError, stamp
from shikiri_yaku.journal.ops import NewOp, OpState
from shikiri_yaku.ports import SubmitOp, SubmitPayload, SubmitStatus

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore, StoredOp
    from shikiri_yaku.ports import Ack

_KIND: Final = "submit"


class JournalSubmits:
    """Submit ops in the ops table; every move is guarded on the expected state."""

    def __init__(self, database: Database, ops: OpStore, *, channel_name: str) -> None:
        self._database = database
        self._ops = ops
        self._channel_name = channel_name

    def intend(self, update_id: int, payload: SubmitPayload, now: datetime) -> SubmitOp:
        return self._intend(_identity(self._channel_name, update_id), payload, now)

    def intend_task(
        self, proposal_id: int, index: int, payload: SubmitPayload, now: datetime
    ) -> None:
        """Journal one task of a proposal; its identity is the proposal and the task's place."""
        self._intend(f"{self._channel_name}:proposal:{proposal_id}:{index}", payload, now)

    def for_proposal(self, proposal_id: int) -> list[SubmitOp]:
        """Return the tasks of a proposal in the order they were journaled."""
        rows = self._database.connection.execute(
            "SELECT id FROM ops WHERE kind = ? AND json_extract(payload, '$.proposal') = ?"
            " ORDER BY id",
            (_KIND, proposal_id),
        ).fetchall()
        return [op for row in rows if (op := self.get(row["id"])) is not None]

    def get(self, op_id: int) -> SubmitOp | None:
        op = self._ops.get(op_id)
        return None if op is None or op.kind != _KIND else _submit_op(op)

    def claim(self, op_id: int, now: datetime) -> bool:
        return self._move(op_id, OpState.INTENDED, OpState.ISSUED, now)

    def confirm(self, op_id: int, ack: Ack, now: datetime) -> None:
        self._move(op_id, OpState.ISSUED, OpState.CONFIRMED, now, ack)

    def fail(self, op_id: int, now: datetime) -> None:
        self._move(op_id, OpState.ISSUED, OpState.FAILED, now)

    def uncertain(self, op_id: int, now: datetime) -> None:
        self._move(op_id, OpState.ISSUED, OpState.UNKNOWN, now)

    def retry(self, prompt_id: int, update_id: int, now: datetime) -> SubmitOp | None:
        """Consume the prompt and intend its replacement, in one transaction with the check.

        The check and the intent are one transaction, so two taps cannot both consume a prompt.
        A replay finds the replacement its tap intended, because the identity is the tap's.
        """
        identity = _identity(self._channel_name, update_id)
        with self._database.transaction():
            taken_by = self._taken_by(prompt_id)
            if taken_by is not None and taken_by != identity:
                return None
            prompt = self.get(prompt_id)
            if prompt is None or prompt.status != SubmitStatus.UNKNOWN:
                return None
            payload = prompt.payload.model_copy(update={"retry_of": prompt_id})
            return self.intend(update_id, payload, now)

    def unfinished(self) -> list[SubmitOp]:
        """Return the goal submits not final; a proposal's tasks are settled by their proposal."""
        ops = [_submit_op(op) for op in self._ops.non_final() if op.kind == _KIND]
        goals = [op for op in ops if op.payload.proposal is None]
        return [op for op in goals if self._taken_by(op.id) is None]

    def _taken_by(self, prompt_id: int) -> str | None:
        """Return the identity of the replacement that consumed the prompt, or None."""
        row = self._database.connection.execute(
            "SELECT identity FROM ops WHERE kind = ? AND json_extract(payload, '$.retry_of') = ?",
            (_KIND, prompt_id),
        ).fetchone()
        return None if row is None else row["identity"]

    def _move(
        self,
        op_id: int,
        expected: OpState,
        new: OpState,
        now: datetime,
        ack: Ack | None = None,
    ) -> bool:
        """Move a submit op only from `expected`; a given acknowledgement is saved with the move."""
        moved = self._database.connection.execute(
            """
            UPDATE ops SET state = :new, updated_at = :now,
                command_id = COALESCE(:command_id, command_id),
                task_id = COALESCE(:task_id, task_id),
                acknowledgement = COALESCE(:acknowledgement, acknowledgement)
            WHERE id = :id AND kind = :kind AND state = :expected
            """,
            {
                "new": new,
                "now": stamp(now),
                "command_id": None if ack is None else str(ack.id),
                "task_id": None if ack is None else ack.task_id,
                "acknowledgement": None if ack is None else ack.model_dump_json(),
                "id": op_id,
                "kind": _KIND,
                "expected": expected,
            },
        ).rowcount
        return moved == 1

    def _intend(self, identity: str, payload: SubmitPayload, now: datetime) -> SubmitOp:
        inserted = self._ops.intend(
            NewOp(
                identity=identity,
                kind=_KIND,
                project=payload.component,
                payload=payload.model_dump_json(),
            ),
            now,
        )
        op = self.get(inserted.row_id)
        if op is None:
            message = f"submit op {inserted.row_id} is missing from the journal"
            raise JournalError(message)
        return op


def _identity(channel_name: str, update_id: int) -> str:
    return f"{channel_name}:submit:{update_id}"


def _submit_op(op: StoredOp) -> SubmitOp:
    if op.payload is None:
        message = f"submit op {op.id} has no payload"
        raise JournalError(message)
    return SubmitOp(
        id=op.id,
        payload=SubmitPayload.model_validate_json(op.payload),
        status=op.state.value,
        task_id=op.task_id,
    )
