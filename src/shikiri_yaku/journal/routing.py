from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.journal.signals import SignalState

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.domain.classification import Classification
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.signals import SignalStore


class JournalRouting:
    """The journal as the Router sees it: a signal moves from new to its end state or to parked."""

    def __init__(self, database: Database, signals: SignalStore) -> None:
        self._database = database
        self._signals = signals

    def begin(
        self,
        number: int,
        classification: Classification | None,
        handler: str | None,
        now: datetime,
    ) -> bool:
        # keep: one transaction, so a signal is never handling without its classification
        with self._database.transaction():
            if not self._signals.transition(number, SignalState.NEW, SignalState.HANDLING, now):
                return False
            if classification is not None:
                self._signals.classify(
                    number,
                    category=classification.category,
                    classified_by=classification.source,
                    verdict=classification.verdict,
                    note=classification.note,
                    handler=handler,
                    now=now,
                )
            return True

    def recover(self, now: datetime) -> int:
        return self._signals.requeue_handling(now)

    def new_signals(self) -> list[tuple[int, Signal]]:
        return [(stored.number, stored.to_signal()) for stored in self._signals.new()]

    def requeue(self, number: int, now: datetime) -> None:
        self._signals.transition(number, SignalState.HANDLING, SignalState.NEW, now)

    def suggestion(self, number: int) -> str:
        stored = self._signals.get(number)
        return "" if stored is None else stored.classification_note or ""

    def resolve(self, number: int, note: str, now: datetime) -> None:
        self._signals.transition(number, SignalState.HANDLING, SignalState.RESOLVED, now, note=note)

    def forwarded(self, number: int, note: str, now: datetime) -> None:
        self._signals.transition(
            number, SignalState.HANDLING, SignalState.FORWARDED, now, note=note
        )

    def park(self, number: int, wake_at: datetime, note: str, now: datetime) -> None:
        self._signals.transition(
            number, SignalState.HANDLING, SignalState.PARKED, now, wake_at=wake_at, note=note
        )
