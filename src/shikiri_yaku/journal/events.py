from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.signal import Closing, Confirmation, Kind, LifecycleState, Signal
from shikiri_yaku.journal.signals import NewSignal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.domain.signal import Lifecycle
    from shikiri_yaku.journal.cursors import CursorStore
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.journal.signals import Change, SignalStore


class JournalEvents:
    """The journal as an event source sees it: domain signals in, cursor rows behind."""

    def __init__(self, database: Database, cursors: CursorStore, ops: OpStore) -> None:
        self._database = database
        self._cursors = cursors
        self._ops = ops

    def last_event_id(self, project: str) -> int | None:
        cursor = self._cursors.get(project)
        return None if cursor is None else cursor.last_event_id

    def record_batch(
        self,
        project: str,
        changes: Sequence[Signal | Closing | Confirmation],
        last_event_id: int,
        lifecycle: Lifecycle | None,
        now: datetime,
        *,
        respected_stop_id: int | None = None,
    ) -> None:
        stored = [_stored(change) for change in changes if not isinstance(change, Confirmation)]
        with self._database.transaction():
            self._cursors.record_batch(
                project, stored, last_event_id, lifecycle, now, respected_stop_id=respected_stop_id
            )
            for change in changes:
                if isinstance(change, Confirmation):
                    self._ops.confirm_command(change.project, change.command_id, now)


def _stored(change: Signal | Closing) -> Change:
    return change if isinstance(change, Closing) else _stored_signal(change)


def _stored_signal(change: Signal) -> NewSignal:
    return NewSignal(
        id=change.id,
        project=change.project,
        kind=change.kind.value,
        reason=change.reason,
        text=change.text,
        task_id=change.task_id,
        escalation_id=change.escalation_id,
    )


class JournalSources:
    """The journal as the liveness and health sources see it."""

    def __init__(self, cursors: CursorStore, signals: SignalStore) -> None:
        self._cursors = cursors
        self._signals = signals

    def state(self, project: str) -> LifecycleState | None:
        cursor = self._cursors.get(project)
        if cursor is None:
            return None
        return LifecycleState(cursor.last_event_id, cursor.lifecycle, cursor.respected_stop_id)

    def respect_stop(self, project: str, event_id: int) -> None:
        self._cursors.respect_stop(project, event_id)

    def open(self, signal: Signal, now: datetime) -> None:
        self._signals.insert_if_absent(_stored_signal(signal), now)

    def has_open(self, project: str, kind: Kind, reason: str | None = None) -> bool:
        return self._signals.has_open(project, kind.value, reason)

    def close(self, closing: Closing, now: datetime) -> None:
        self._signals.close(closing, now)
