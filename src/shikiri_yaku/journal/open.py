"""What /open shows again: forwarded questions and approvals awaiting Confirm (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.ports import PendingApproval, ResolutionKind, ResolvePayload

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.signals import SignalStore

_APPROVALS = (ResolutionKind.ACCEPT, ResolutionKind.CANCEL)


class JournalOpen:
    """The journal side of OpenLog; it reads the signals and the resolve ops and writes nothing."""

    def __init__(self, database: Database, signals: SignalStore) -> None:
        self._database = database
        self._signals = signals

    def forwarded(self) -> list[tuple[int, Signal]]:
        return [
            (stored.number, stored.to_signal()) for stored in self._signals.forwarded_escalations()
        ]

    def pending_approvals(self) -> list[PendingApproval]:
        rows = self._database.connection.execute(
            """
            SELECT id, payload FROM ops
            WHERE kind = 'resolve' AND state IN (?, ?) AND payload IS NOT NULL ORDER BY id
            """,
            (OpState.INTENDED, OpState.APPROVED),
        ).fetchall()
        found: list[PendingApproval] = []
        for row in rows:
            payload = ResolvePayload.model_validate_json(row["payload"])
            if payload.kind in _APPROVALS:
                found.append(
                    PendingApproval(
                        row["id"],
                        payload.number,
                        payload.project,
                        payload.escalation_id,
                        payload.kind,
                    )
                )
        return found
