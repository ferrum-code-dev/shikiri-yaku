from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.signal import Closing, Signal
from shikiri_yaku.journal.signals import NewSignal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.domain.signal import Lifecycle
    from shikiri_yaku.journal.cursors import CursorStore
    from shikiri_yaku.journal.signals import Change


class JournalEvents:
    """The journal as an event source sees it: domain signals in, cursor rows behind."""

    def __init__(self, cursors: CursorStore) -> None:
        self._cursors = cursors

    def last_event_id(self, project: str) -> int | None:
        cursor = self._cursors.get(project)
        return None if cursor is None else cursor.last_event_id

    def record_batch(
        self,
        project: str,
        changes: Sequence[Signal | Closing],
        last_event_id: int,
        lifecycle: Lifecycle | None,
        now: datetime,
        *,
        respected_stop_id: int | None = None,
    ) -> None:
        stored = [_stored(change) for change in changes]
        self._cursors.record_batch(
            project, stored, last_event_id, lifecycle, now, respected_stop_id=respected_stop_id
        )


def _stored(change: Signal | Closing) -> Change:
    if isinstance(change, Closing):
        return change
    return NewSignal(
        id=change.id,
        project=change.project,
        kind=change.kind.value,
        reason=change.reason,
        text=change.text,
        task_id=change.task_id,
        escalation_id=change.escalation_id,
    )
