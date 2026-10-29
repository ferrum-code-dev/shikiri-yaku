from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.journal.ops import NewOp, OpState, OpStore
from shikiri_yaku.journal.signals import SignalState
from shikiri_yaku.ports import (
    Message,
    RecordedResolution,
    ReplyOrigin,
    ResolutionKind,
    ResolvePayload,
)

if TYPE_CHECKING:
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
            Destination(row["channel"], row["chat"]),
        )

    def sent_message(self, destination: Destination, message_ref: str) -> Message | None:
        row = self._database.connection.execute(
            """
            SELECT body FROM messages WHERE channel = ? AND chat = ? AND platform_message_id = ?
                AND direction = ? AND delivery = ?
            """,
            (destination.channel, destination.chat, message_ref, Direction.OUT, Delivery.SENT),
        ).fetchone()
        return None if row is None or row["body"] is None else _BODY.validate_json(row["body"])

    def recorded_resolution(self, number: int) -> RecordedResolution | None:
        row = self._database.connection.execute(
            """
            SELECT payload, updated_at FROM ops
            WHERE kind = ? AND state = ? AND json_extract(payload, '$.number') = ?
            ORDER BY id DESC LIMIT 1
            """,
            (_RESOLVE, OpState.CONFIRMED, number),
        ).fetchone()
        if row is None:
            return None
        payload = ResolvePayload.model_validate_json(row["payload"])
        # keep: rows journaled before the label existed are named by their text, or else their kind
        label = payload.label or payload.text or payload.kind
        return RecordedResolution(label, datetime.fromisoformat(row["updated_at"]))

    def intend_resolve(self, update_id: int, payload: ResolvePayload, now: datetime) -> int | None:
        form = secret_form([payload.text or ""])
        inserted = self._ops.intend(
            NewOp(
                identity=f"telegram:resolve:{update_id}",
                kind=_RESOLVE,
                project=payload.project,
                task_id=payload.task_id,
                reason=payload.reason,
                payload=payload.model_dump_json()
                if form is None
                else json.dumps({"refusal": form}),
                state=OpState.INTENDED if form is None else OpState.FAILED,
            ),
            now,
        )
        return inserted.row_id if form is None else None

    def approve_resolve(self, op_id: int, number: int, now: datetime) -> bool:
        with self._database.transaction():
            op = self._ops.get(op_id)
            if (
                op is None
                or op.kind != _RESOLVE
                or op.state not in {OpState.INTENDED, OpState.APPROVED}
                or op.payload is None
            ):
                return False
            payload = ResolvePayload.model_validate_json(op.payload)
            if payload.number != number or payload.kind not in {
                ResolutionKind.ACCEPT,
                ResolutionKind.CANCEL,
            }:
                return False
            return op.state == OpState.APPROVED or self._ops.transition(
                op_id, OpState.INTENDED, OpState.APPROVED, now
            )

    def claim_resolve(self, op_id: int, now: datetime) -> ResolvePayload | None:
        with self._database.transaction():
            op = self._ops.get(op_id)
            if (
                op is None
                or op.kind != _RESOLVE
                or op.state not in {OpState.INTENDED, OpState.APPROVED}
                or op.payload is None
            ):
                return None
            payload = ResolvePayload.model_validate_json(op.payload)
            expected = (
                OpState.APPROVED
                if payload.kind in {ResolutionKind.ACCEPT, ResolutionKind.CANCEL}
                else OpState.INTENDED
            )
            if not self._ops.transition(op_id, expected, OpState.ISSUED, now):
                return None
            self._signals.transition(
                payload.number, SignalState.FORWARDED, SignalState.ANSWERED, now
            )
            return payload

    def acknowledge_resolve(self, op_id: int, ack: Ack, now: datetime) -> None:
        self._database.connection.execute(
            """
            UPDATE ops SET acknowledgement = ?, command_id = ?, updated_at = ?
            WHERE id = ? AND kind = 'resolve' AND state = ? AND acknowledgement IS NULL
            """,
            (ack.model_dump_json(), str(ack.id), stamp(now), op_id, OpState.ISSUED),
        )

    def reject_resolve(self, op_id: int, now: datetime) -> bool:
        return self._ops.transition(op_id, OpState.ISSUED, OpState.FAILED, now)

    def acknowledge_notice(self, number: int, now: datetime) -> None:
        self._signals.transition(number, SignalState.FORWARDED, SignalState.CLOSED, now)
