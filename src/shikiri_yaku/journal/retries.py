from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.ops import NewOp, OpState
from shikiri_yaku.ports import (
    ResolutionKind,
    ResolvePayload,
    RetryAttempt,
)

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.ports import Ack

_RESOLVE: Final = "resolve"


class JournalRetries:
    """Automatic retries journaled as resolve ops, so the events and the reconciler settle them."""

    def __init__(self, database: Database, ops: OpStore) -> None:
        self._database = database
        self._ops = ops

    def existing(self, signal: Signal) -> RetryAttempt | None:
        row = self._database.connection.execute(
            "SELECT state FROM ops WHERE identity = ? AND kind = ?",
            (f"retry:{signal.id}", _RESOLVE),
        ).fetchone()
        return None if row is None else RetryAttempt(row["state"])

    def issue(self, signal: Signal, escalation_id: str, now: datetime) -> int | None:
        row = self._database.connection.execute(
            "SELECT number FROM signals WHERE id = ?", (signal.id,)
        ).fetchone()
        if row is None:
            raise LookupError(signal.id)
        # keep: the reconciler finds the signal by number, which only the journal can look up
        payload = ResolvePayload(
            number=row["number"],
            project=signal.project,
            task_id=signal.task_id,
            reason=signal.reason,
            escalation_id=escalation_id,
            kind=ResolutionKind.RETRY,
            text=None,
            label=ResolutionKind.RETRY,
        )
        inserted = self._ops.intend(
            NewOp(
                identity=f"retry:{signal.id}",
                kind=_RESOLVE,
                project=signal.project,
                task_id=signal.task_id,
                reason=signal.reason,
                payload=payload.model_dump_json(),
                state=OpState.ISSUED,
            ),
            now,
        )
        return inserted.row_id if inserted.is_new else None

    def acknowledge(self, op_id: int, ack: Ack, now: datetime) -> None:
        self._ops.acknowledge_resolve(op_id, ack, now)

    def reject(self, op_id: int, now: datetime) -> None:
        self._ops.transition(op_id, OpState.ISSUED, OpState.FAILED, now)
