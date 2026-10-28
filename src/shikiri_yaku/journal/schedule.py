from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.schedule import Awaiting, Destination, Outgoing, Parked
from shikiri_yaku.journal.database import parse_stamp
from shikiri_yaku.journal.messages import Delivery, Direction, Purpose
from shikiri_yaku.journal.ops import NewOp, OpState
from shikiri_yaku.journal.signals import SignalState

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.messages import MessageStore
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.journal.signals import SignalStore, StoredSignal

REMIND: Final = "remind"


def _parked(stored: StoredSignal) -> Parked:
    return Parked(stored.number, stored.handler or "", stored.to_signal())


class JournalSchedule:
    """The journal as the Scheduler sees it: parked signals, unanswered questions and reminders."""

    def __init__(
        self,
        database: Database,
        signals: SignalStore,
        messages: MessageStore,
        ops: OpStore,
        *,
        reminder_notice_kinds: frozenset[str],
    ) -> None:
        self._database = database
        self._signals = signals
        self._messages = messages
        self._ops = ops
        self._reminder_notice_kinds = reminder_notice_kinds

    def due_parked(self, now: datetime) -> list[Parked]:
        return [_parked(stored) for stored in self._signals.parked_due(now)]

    def claim(self, number: int, now: datetime) -> bool:
        return self._signals.transition(number, SignalState.PARKED, SignalState.HANDLING, now)

    def awaiting_answer(self) -> list[Awaiting]:
        rows = self._database.connection.execute(
            """
            SELECT s.number, s.project, s.task_id, s.kind, m.purpose,
                   m.channel, m.chat, m.created_at
            FROM signals s JOIN messages m ON m.signal_number = s.number
            WHERE s.state = :forwarded AND m.direction = :out AND m.delivery = :sent
                AND (m.purpose IN (:question, :approval)
                     OR (m.purpose = :notice
                         AND (m.notice_key IS NULL OR m.notice_key NOT LIKE :reminders)))
            ORDER BY s.number, m.id
            """,
            {
                "forwarded": SignalState.FORWARDED,
                "out": Direction.OUT,
                "sent": Delivery.SENT,
                "question": Purpose.QUESTION,
                "approval": Purpose.APPROVAL,
                "notice": Purpose.NOTICE,
                "reminders": f"{REMIND}:%",
            },
        )
        grouped: dict[int, Awaiting] = {}
        for row in rows:
            if row["purpose"] == Purpose.NOTICE and row["kind"] not in self._reminder_notice_kinds:
                continue
            asked_at = parse_stamp(row["created_at"])
            if asked_at is None:
                continue
            where = Destination(row["channel"], row["chat"])
            known = grouped.get(row["number"])
            if known is None:
                grouped[row["number"]] = Awaiting(
                    row["number"], row["project"], row["task_id"], asked_at, (where,)
                )
            else:
                grouped[row["number"]] = replace(
                    known,
                    asked_at=min(known.asked_at, asked_at),
                    destinations=known.destinations
                    if where in known.destinations
                    else (*known.destinations, where),
                )
        return list(grouped.values())

    def schedule_reminder(
        self, awaiting: Awaiting, occurrence: int, now: datetime
    ) -> list[Outgoing]:
        # keep: one transaction, so an interruption leaves neither the op nor a partial outbox
        identity = f"{REMIND}:{awaiting.number}:{occurrence}"
        op = NewOp(identity, REMIND, awaiting.project, awaiting.task_id, state=OpState.CONFIRMED)
        with self._database.transaction():
            if not self._ops.intend(op, now).is_new:
                return []
            return [
                Outgoing(
                    self._messages.record_outbound(
                        signal_number=awaiting.number,
                        channel=where.channel,
                        chat=where.chat,
                        purpose=Purpose.NOTICE,
                        notice_key=identity,
                        now=now,
                    ).row_id,
                    where,
                )
                for where in awaiting.destinations
            ]

    def delivered(self, outgoing: Outgoing, message_ref: str) -> None:
        self._messages.confirm_delivery(outgoing.message_id, message_ref)
