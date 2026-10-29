from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.journal.signals import SignalState
from shikiri_yaku.ports import ResolvePayload, Unsettled

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.journal.signals import SignalStore

_RESOLVE: Final = "resolve"


class JournalReconcile:
    def __init__(self, database: Database, signals: SignalStore, ops: OpStore) -> None:
        self._database = database
        self._signals = signals
        self._ops = ops

    def unsettled(self) -> list[Unsettled]:
        found: list[Unsettled] = []
        for op in self._ops.non_final():
            signal = None
            payload = None
            if op.kind == _RESOLVE and op.state == OpState.ISSUED and op.payload is not None:
                payload = ResolvePayload.model_validate_json(op.payload)
                signal = self._signals.get(payload.number)
            if payload is not None and signal is not None:
                found.append(
                    Unsettled(op.id, payload, signal.to_signal(), op.acknowledgement, op.updated_at)
                )
        return found

    def confirm(self, op_id: int, now: datetime) -> bool:
        return self._ops.transition(op_id, OpState.ISSUED, OpState.CONFIRMED, now)

    def fail(self, op_id: int, now: datetime, *, question_shown: bool = False) -> bool:
        with self._database.transaction():
            op = self._ops.get(op_id)
            if op is None or op.payload is None:
                return False
            if not self._ops.transition(op_id, OpState.ISSUED, OpState.FAILED, now):
                return False
            number = ResolvePayload.model_validate_json(op.payload).number
            if question_shown:
                # keep: the keyed replacement question owns this signal after a crash
                for state in (
                    SignalState.ANSWERED,
                    SignalState.RESOLVED,
                    SignalState.HANDLING,
                    SignalState.NEW,
                ):
                    if self._signals.transition(number, state, SignalState.FORWARDED, now):
                        break
                return True
            if not self._signals.transition(
                number, SignalState.ANSWERED, SignalState.FORWARDED, now
            ):
                # keep: a lost automatic retry's signal was resolved by the Router, not answered
                self._signals.transition(number, SignalState.RESOLVED, SignalState.FORWARDED, now)
            return True
