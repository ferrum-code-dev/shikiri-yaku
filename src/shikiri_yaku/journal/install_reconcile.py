"""The journal steps of settling an install a restart found unsettled.

Each step moves the op only from the state the reconciler read, so a second reader of the same op
changes it once. The run steps in `install_runs` are not used here: a restart has no claim to make.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.ops import INSTALL_KIND, OpState
from shikiri_yaku.ports import PendingInstall

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore, StoredOp
    from shikiri_yaku.journal.signals import SignalStore
    from shikiri_yaku.ports import Ack

_UNSETTLED: Final = (OpState.APPROVED, OpState.ISSUED)


class JournalInstallReconcile:
    """Implements `InstallReconcileLog` over the ops and signals tables."""

    def __init__(self, database: Database, signals: SignalStore, ops: OpStore) -> None:
        self._database = database
        self._signals = signals
        self._ops = ops

    def unsettled(self) -> list[PendingInstall]:
        found = [self._pending(op) for op in self._ops.non_final()]
        return [pending for pending in found if pending is not None]

    def unsettled_op(self, op_id: int) -> PendingInstall | None:
        op = self._ops.get(op_id)
        return None if op is None else self._pending(op)

    def acknowledge(self, op_id: int, ack: Ack, final_line: str, now: datetime) -> None:
        with self._database.transaction():
            operation = self._ops.install_op(op_id)
            if operation is None or operation.state != OpState.ISSUED:
                return
            event_arrived = self._ops.acknowledge_install(op_id, ack, None, now)
            self._signals.set_final_line(
                operation.payload.number, final_line, now, replace=event_arrived
            )

    def confirm(self, op_id: int, final_line: str, now: datetime) -> bool:
        with self._database.transaction():
            operation = self._ops.install_op(op_id)
            if operation is None or not self._ops.transition(
                op_id, OpState.ISSUED, OpState.CONFIRMED, now
            ):
                return False
            self._signals.set_final_line(operation.payload.number, final_line, now, replace=True)
            return True

    def record_rejection(self, op_id: int, reason: str, now: datetime) -> bool:
        return self._ops.record_install_rejection(op_id, reason, now)

    def void_approved(self, op_id: int, now: datetime) -> bool:
        return self._ops.transition(op_id, OpState.APPROVED, OpState.VOID, now)

    def void(self, op_id: int, now: datetime) -> bool:
        return self._ops.void_issued(op_id, now)

    def fail(self, op_id: int, now: datetime) -> bool:
        return self._ops.transition(op_id, OpState.ISSUED, OpState.FAILED, now)

    def check_failed(self, op_id: int, now: datetime) -> None:
        self._ops.mark_install_check_failed(op_id, now)

    def _pending(self, op: StoredOp) -> PendingInstall | None:
        if op.kind != INSTALL_KIND or op.state not in _UNSETTLED:
            return None
        operation = self._ops.install_op(op.id)
        if operation is None:
            return None
        stored = self._signals.get(operation.payload.number)
        if stored is None:
            return None
        return PendingInstall(
            operation,
            stored.to_signal(),
            op.acknowledgement,
            op.updated_at,
            op.resolved_command_id,
            op.install_rejection,
        )
