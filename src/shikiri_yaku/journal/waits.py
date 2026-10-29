"""Quota waits as wait ops: the op, not memory, says when a parked signal is due (§6.2)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- pydantic reads the annotations at runtime
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.ops import NewOp, OpState
from shikiri_yaku.ports import WaitAttempt

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore

_KIND: Final = "wait"


class WaitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    wake_at: datetime


class JournalWaits:
    """Wait ops in the ops table, one per signal; recording the same wait again changes nothing."""

    def __init__(self, database: Database, ops: OpStore) -> None:
        self._database = database
        self._ops = ops

    def existing(self, signal: Signal) -> WaitAttempt | None:
        row = self._database.connection.execute(
            "SELECT payload FROM ops WHERE identity = ? AND kind = ?",
            (_identity(signal), _KIND),
        ).fetchone()
        if row is None:
            return None
        return WaitAttempt(WaitPayload.model_validate_json(row["payload"]).wake_at)

    def record(self, signal: Signal, wake_at: datetime, now: datetime) -> None:
        self._ops.intend(
            NewOp(
                identity=_identity(signal),
                kind=_KIND,
                project=signal.project,
                task_id=signal.task_id,
                reason=signal.reason,
                payload=WaitPayload(wake_at=wake_at).model_dump_json(),
                state=OpState.ISSUED,
            ),
            now,
        )

    def finish(self, signal: Signal, now: datetime) -> None:
        self._database.connection.execute(
            """
            UPDATE ops SET state = ?, updated_at = ?
            WHERE identity = ? AND kind = ? AND state = ?
            """,
            (OpState.CONFIRMED, stamp(now), _identity(signal), _KIND, OpState.ISSUED),
        )


def _identity(signal: Signal) -> str:
    return f"wait:{signal.id}"
