"""The event source of one project: Shikiru's events become signals (design §4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from shikiri_yaku.domain.signal import Closing, Kind, Lifecycle, Signal
from shikiri_yaku.ports import (
    EscalationStatus,
    EscalationView,
    EventKinds,
    ShikiruEvent,
    ShikiruProtocolError,
)

if TYPE_CHECKING:
    from shikiri_yaku.ports import Clock, EventJournal, ShikiruClient

STOP_REASON_KEY = "reason"
PR_CLOSED_REASON = "pr_closed"
NO_EVENTS = 0

type Change = Signal | Closing


class _ResolvedData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    escalation_id: str


class _EscalationData(_ResolvedData):
    reason: str


class EventSource:
    """Reads one project's events; the supervisor calls `poll` again after each return.

    A failure of the link crosses as the client's typed exception: the supervisor owns the
    reconnect and the health signal.
    """

    def __init__(
        self,
        project: str,
        client: ShikiruClient,
        journal: EventJournal,
        clock: Clock,
        wait_s: float,
    ) -> None:
        self._project = project
        self._client = client
        self._journal = journal
        self._clock = clock
        self._wait_s = wait_s

    async def poll(self, *, wait_s: float | None = None) -> bool:
        """Record one batch; return whether another immediate poll may have more events."""
        cursor = self._journal.last_event_id(self._project)
        if cursor is None:
            await self._first_sync()
            return True
        events = await self._client.wait_for_events(
            cursor, self._wait_s if wait_s is None else wait_s
        )
        if not events:
            return False
        if events[0].id > cursor + 1:
            await self._first_sync()
            return True
        changes: list[Change] = []
        for event in events:
            changes.extend(await self._changes(event))
        self._journal.record_batch(
            self._project, changes, events[-1].id, _lifecycle(events), self._clock.now()
        )
        return True

    async def _first_sync(self) -> None:
        """Read the whole history for its lifecycle only, then open the live escalations.

        The events come before the escalation list: one that is raised in between is read again
        from the cursor and recognised by id.
        """
        last_id = NO_EVENTS
        lifecycle = None
        while events := await self._client.wait_for_events(last_id, 0):
            last_id = events[-1].id
            lifecycle = _lifecycle(events) or lifecycle
        views = await self._client.list_escalations()
        changes = [
            _escalation_signal(self._project, view.escalation.task_id, view)
            for view in views
            if view.escalation.status == EscalationStatus.OPEN
        ]
        clean_stop = (
            lifecycle is not None
            and lifecycle.kind == EventKinds.ENGINE_STOPPED
            and not lifecycle.has_reason
        )
        respected = lifecycle.event_id if clean_stop and lifecycle is not None else None
        self._journal.record_batch(
            self._project,
            changes,
            last_id,
            lifecycle,
            self._clock.now(),
            respected_stop_id=respected,
        )

    async def _changes(self, event: ShikiruEvent) -> list[Change]:
        match event.kind:
            case EventKinds.ESCALATION:
                return await self._escalation(event)
            case EventKinds.ESCALATION_RESOLVED:
                data = _event_data(event, _ResolvedData)
                return [Closing(self._project, escalation_id=data.escalation_id)]
            case EventKinds.ENGINE_STOPPED if STOP_REASON_KEY in event.data:
                return [self._signal(event, Kind.ENGINE_FAULT, str(event.data[STOP_REASON_KEY]))]
            case _:
                return self._task_changes(event)

    def _task_changes(self, event: ShikiruEvent) -> list[Change]:
        pr_closed = Closing(self._project, kind=Kind.PR_REVIEW, task_id=event.task_id)
        match event.kind:
            case EventKinds.TASK_DONE:
                return [self._signal(event, Kind.TASK_DONE, event.kind), pr_closed]
            case EventKinds.PR_OPENED:
                return [self._signal(event, Kind.PR_REVIEW, event.kind)]
            case EventKinds.PR_WITHDRAWN:
                return [pr_closed]
            case _:
                return []

    async def _escalation(self, event: ShikiruEvent) -> list[Change]:
        data = _event_data(event, _EscalationData)
        view = await self._client.get_escalation(data.escalation_id)
        signal = Signal(
            self._project,
            Kind.ESCALATION,
            data.escalation_id,
            data.reason,
            event.message if view is None else _escalation_text(view),
            task_id=event.task_id,
            escalation_id=data.escalation_id,
        )
        changes: list[Change] = [signal]
        if data.reason == PR_CLOSED_REASON:
            changes.append(Closing(self._project, kind=Kind.PR_REVIEW, task_id=event.task_id))
        return changes

    def _signal(self, event: ShikiruEvent, kind: Kind, reason: str) -> Signal:
        return Signal(
            self._project, kind, str(event.id), reason, event.message, task_id=event.task_id
        )


def _event_data[T: _ResolvedData](event: ShikiruEvent, model: type[T]) -> T:
    try:
        return model.model_validate(event.data)
    except ValidationError as error:
        message = f"event {event.id} ({event.kind}) lacks its data: {error}"
        raise ShikiruProtocolError(message) from error


def _escalation_text(view: EscalationView) -> str:
    headline = view.escalation.headline
    return headline if not view.question else f"{headline}\n\n{view.question}"


def _escalation_signal(project: str, task_id: str, view: EscalationView) -> Signal:
    escalation = view.escalation
    return Signal(
        project,
        Kind.ESCALATION,
        escalation.id,
        escalation.reason,
        _escalation_text(view),
        task_id=task_id,
        escalation_id=escalation.id,
    )


def _lifecycle(events: list[ShikiruEvent]) -> Lifecycle | None:
    """Return the newest start or stop in the batch."""
    for event in reversed(events):
        if event.kind in {EventKinds.ENGINE_STARTED, EventKinds.ENGINE_STOPPED}:
            return Lifecycle(
                event.id, event.kind, has_reason=STOP_REASON_KEY in event.data, at=event.ts
            )
    return None
