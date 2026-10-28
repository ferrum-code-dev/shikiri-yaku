from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.journal.ops import NewOp, OpState, OpStore
from shikiri_yaku.ports import Message, ReplyOrigin, ResolvePayload

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.signals import SignalStore
    from shikiri_yaku.ports import Ack


_RESOLVE: Final = "resolve"
_BODY = TypeAdapter(Message)


class JournalReplies:
    def __init__(self, database: Database, signals: SignalStore, ops: OpStore) -> None:
        self._database = database
        self._signals = signals
        self._ops = ops

    def origin(self, channel: str, message_ref: str) -> ReplyOrigin | None:
        row = self._database.connection.execute(
            """
            SELECT * FROM messages WHERE channel = ? AND platform_message_id = ?
                AND direction = ? AND delivery = ?
            """,
            (channel, message_ref, Direction.OUT, Delivery.SENT),
        ).fetchone()
        if row is None:
            return None
        signal = None if row["signal_number"] is None else self._signals.get(row["signal_number"])
        if signal is None or row["purpose"] is None or row["body"] is None:
            return None
        return ReplyOrigin(
            signal.number,
            signal.to_signal(),
            Purpose(row["purpose"]),
            _BODY.validate_json(row["body"]),
            row["notice_key"],
        )

    def intend_resolve(self, update_id: int, payload: ResolvePayload, now: datetime) -> int:
        return self._ops.intend(
            NewOp(
                identity=f"telegram:resolve:{update_id}",
                kind=_RESOLVE,
                project=payload.project,
                task_id=payload.task_id,
                reason=payload.reason,
                payload=payload.model_dump_json(),
            ),
            now,
        ).row_id

    def claim_resolve(self, op_id: int, now: datetime) -> ResolvePayload | None:
        with self._database.transaction():
            op = self._ops.get(op_id)
            if op is None or op.kind != _RESOLVE or op.payload is None:
                return None
            payload = ResolvePayload.model_validate_json(op.payload)
            if not self._ops.transition(op_id, OpState.INTENDED, OpState.ISSUED, now):
                return None
            return payload

    def acknowledge_resolve(self, op_id: int, ack: Ack, now: datetime) -> None:
        self._database.connection.execute(
            """
            UPDATE ops SET acknowledgement = ?, command_id = ?, updated_at = ?
            WHERE id = ? AND kind = 'resolve' AND state = ? AND acknowledgement IS NULL
            """,
            (ack.model_dump_json(), str(ack.id), stamp(now), op_id, OpState.ISSUED),
        )
