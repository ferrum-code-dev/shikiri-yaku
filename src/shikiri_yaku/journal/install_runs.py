"""The journal steps of an install run (design §6.3, §9): claimed, then acknowledged or failed.

Each step is one conditional change, so two executors that meet on one op change it once. The op
is confirmed by the event that names its command, in the event journal (design §10.1), not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.ports import InstallClaim

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.journal.signals import SignalStore
    from shikiri_yaku.ports import Ack


class JournalInstallRuns:
    """Implements `InstallRunLog` over the ops and signals tables."""

    def __init__(self, database: Database, signals: SignalStore, ops: OpStore) -> None:
        self._database = database
        self._signals = signals
        self._ops = ops

    def claim(self, op_id: int, now: datetime) -> InstallClaim | None:
        with self._database.transaction():
            operation = self._ops.install_op(op_id)
            if operation is None or operation.state != OpState.APPROVED:
                return None
            stored = self._signals.get(operation.payload.number)
            if stored is None:
                return None
            if not self._ops.transition(op_id, OpState.APPROVED, OpState.ISSUED, now):
                return None
            return InstallClaim(operation, stored.to_signal(), stored.classification_note or "")

    def acknowledge(
        self, op_id: int, ack: Ack, output_path: str, final_line: str, now: datetime
    ) -> None:
        with self._database.transaction():
            operation = self._ops.install_op(op_id)
            if operation is None or operation.state != OpState.ISSUED:
                return
            self._ops.acknowledge_install(op_id, ack, output_path, now)
            self._signals.set_final_line(operation.payload.number, final_line, now)

    def fail(self, op_id: int, output_path: str, now: datetime) -> bool:
        return self._ops.transition(
            op_id, OpState.ISSUED, OpState.FAILED, now, output_path=output_path
        )
