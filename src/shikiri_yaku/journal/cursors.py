from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shikiri_yaku.domain.signal import Lifecycle
from shikiri_yaku.journal.signals import NewSignal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.journal.database import Database, Inserted
    from shikiri_yaku.journal.signals import Change, SignalStore


@dataclass(frozen=True)
class Cursor:
    project: str
    last_event_id: int
    lifecycle: Lifecycle | None
    respected_stop_id: int | None


class CursorStore:
    def __init__(self, database: Database, signals: SignalStore) -> None:
        self._database = database
        self._signals = signals

    def get(self, project: str) -> Cursor | None:
        row = self._database.connection.execute(
            "SELECT * FROM cursors WHERE project = ?", (project,)
        ).fetchone()
        if row is None:
            return None
        lifecycle = None
        if row["lifecycle_event_id"] is not None:
            lifecycle = Lifecycle(
                row["lifecycle_event_id"],
                row["lifecycle_kind"],
                has_reason=bool(row["lifecycle_has_reason"]),
            )
        return Cursor(project, row["last_event_id"], lifecycle, row["respected_stop_id"])

    def record_batch(
        self,
        project: str,
        changes: Sequence[Change],
        last_event_id: int,
        lifecycle: Lifecycle | None,
        now: datetime,
    ) -> list[Inserted]:
        """Apply the changes of one batch of events and advance the cursor, in one transaction.

        The changes go first and in event order, so a crash between them and the cursor leaves
        the cursor behind and the batch is read again; the signals are then recognised by id.
        Returns what each new signal's insert gave, in order.
        """
        with self._database.transaction() as connection:
            inserted: list[Inserted] = []
            for change in changes:
                if isinstance(change, NewSignal):
                    inserted.append(self._signals.insert_if_absent(change, now))
                else:
                    self._signals.close(change, now)
            connection.execute(
                """
                INSERT INTO cursors (project, last_event_id, lifecycle_event_id, lifecycle_kind,
                                     lifecycle_has_reason)
                VALUES (:project, :last_event_id, :event_id, :kind, :has_reason)
                ON CONFLICT (project) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    lifecycle_event_id = COALESCE(excluded.lifecycle_event_id, lifecycle_event_id),
                    lifecycle_kind = COALESCE(excluded.lifecycle_kind, lifecycle_kind),
                    lifecycle_has_reason =
                        COALESCE(excluded.lifecycle_has_reason, lifecycle_has_reason)
                """,
                {
                    "project": project,
                    "last_event_id": last_event_id,
                    "event_id": None if lifecycle is None else lifecycle.event_id,
                    "kind": None if lifecycle is None else lifecycle.kind,
                    "has_reason": None if lifecycle is None else int(lifecycle.has_reason),
                },
            )
            return inserted

    def respect_stop(self, project: str, event_id: int) -> None:
        """Remember a clean stop that was respected, so it never triggers a restart."""
        self._database.connection.execute(
            "UPDATE cursors SET respected_stop_id = ? WHERE project = ?", (event_id, project)
        )
