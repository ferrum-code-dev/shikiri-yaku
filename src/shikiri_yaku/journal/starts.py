"""Engine starts as start ops, so a restart finds the op instead of starting again."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.ops import NewOp, OpState
from shikiri_yaku.ports import StartAttempt, UnsettledStart

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.journal.signals import SignalStore

_KIND: Final = "start"


class JournalStarts:
    """Start ops in the ops table, one per signal; a signal that already has one starts nothing."""

    def __init__(self, database: Database, ops: OpStore, signals: SignalStore) -> None:
        self._database = database
        self._ops = ops
        self._signals = signals

    def existing(self, signal: Signal) -> StartAttempt | None:
        row = self._database.connection.execute(
            "SELECT state FROM ops WHERE identity = ? AND kind = ?",
            (_identity(signal), _KIND),
        ).fetchone()
        return None if row is None else StartAttempt(row["state"])

    def issue(self, signal: Signal, now: datetime) -> None:
        self._ops.intend(
            NewOp(
                identity=_identity(signal),
                kind=_KIND,
                project=signal.project,
                task_id=signal.task_id,
                reason=signal.reason,
                state=OpState.ISSUED,
            ),
            now,
        )

    def confirm(self, signal: Signal, now: datetime) -> None:
        self._move(signal, OpState.CONFIRMED, now)

    def fail(self, signal: Signal, now: datetime) -> None:
        self._move(signal, OpState.FAILED, now)

    def unsettled(self) -> list[UnsettledStart]:
        rows = self._database.connection.execute(
            "SELECT identity, updated_at FROM ops WHERE kind = ? AND state = ? ORDER BY id",
            (_KIND, OpState.ISSUED),
        ).fetchall()
        found: list[UnsettledStart] = []
        for row in rows:
            stored = self._signals.by_id(row["identity"].removeprefix(f"{_KIND}:"))
            if stored is not None:
                found.append(
                    UnsettledStart(stored.to_signal(), datetime.fromisoformat(row["updated_at"]))
                )
        return found

    def _move(self, signal: Signal, state: OpState, now: datetime) -> None:
        self._database.connection.execute(
            """
            UPDATE ops SET state = ?, updated_at = ?
            WHERE identity = ? AND kind = ? AND state = ?
            """,
            (state, stamp(now), _identity(signal), _KIND, OpState.ISSUED),
        )


def _identity(signal: Signal) -> str:
    return f"{_KIND}:{signal.id}"
