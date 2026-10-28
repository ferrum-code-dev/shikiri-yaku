"""The ports: what the core asks of the outside world (design §12).

The core and the extensions depend on these protocols only. Adapters implement them, and
bootstrap.py is the one place that chooses an implementation. A port fails with one of the typed
exceptions below; only the Router turns such a failure into a Forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 -- pydantic reads the annotations at runtime
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import BaseModel, ConfigDict

from shikiri_yaku.domain.classification import Classifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.classification import Assessment, Classification
    from shikiri_yaku.domain.loop_guard import AutoOp, Cause
    from shikiri_yaku.domain.schedule import Awaiting, Outgoing, Parked
    from shikiri_yaku.domain.signal import Closing, Kind, Lifecycle, LifecycleState, Signal

__all__ = [
    "Ack",
    "AckStatus",
    "ActionLog",
    "AgentOption",
    "Channel",
    "ChannelError",
    "Classifier",
    "Clock",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "CompositionError",
    "DispatchLog",
    "EngineStatus",
    "EscalationRecord",
    "EscalationResolution",
    "EscalationStatus",
    "EscalationView",
    "EventJournal",
    "EventKinds",
    "Forwarder",
    "LifecycleJournal",
    "LivenessJournal",
    "QuotaWindow",
    "Reasoner",
    "ReasonerError",
    "ResolutionKind",
    "RoutingLog",
    "ScheduleLog",
    "ShikiruClient",
    "ShikiruError",
    "ShikiruEvent",
    "ShikiruLinkDownError",
    "ShikiruProtocolError",
    "ShikiruRejectedError",
    "ShikiruTask",
    "SignalLog",
    "TaskDetail",
    "TaskStatus",
    "WakeTarget",
]


class _ShikiruPayload(BaseModel):
    """A payload of Shikiru's MCP server (design §11).

    Only the fields the Operator reads are declared, and none has a default: a declared field that
    is missing or has the wrong type fails as a ShikiruProtocolError. Fields Shikiru adds are
    ignored, so Shikiru can grow without breaking the Operator.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


class QuotaWindow(_ShikiruPayload):
    name: str
    used_pct: float
    resets_at: datetime | None


class EngineStatus(_ShikiruPayload):
    engine_up: bool
    quotas: dict[str, list[QuotaWindow]]


class ShikiruEvent(_ShikiruPayload):
    """One event of a project's log; `kind` stays text because Shikiru adds kinds."""

    id: int
    ts: datetime
    task_id: str | None
    kind: str
    message: str
    data: dict[str, Any]


class EventKinds:
    """Event kinds the Operator acts on (design §4). Shikiru adds kinds, so these are text."""

    ESCALATION: Final = "escalation"
    ESCALATION_RESOLVED: Final = "escalation_resolved"
    TASK_DONE: Final = "task_done"
    PR_OPENED: Final = "pr_opened"
    PR_WITHDRAWN: Final = "pr_withdrawn"
    ENGINE_STARTED: Final = "engine_started"
    ENGINE_STOPPED: Final = "engine_stopped"


class EscalationStatus:
    """Escalation states the Operator acts on. Shikiru may add more, so these are text."""

    OPEN: Final = "open"
    RESOLVED: Final = "resolved"


class ResolutionKind:
    """Resolutions `resolve_escalation` takes (design §10.1). Shikiru may add kinds."""

    ANSWER: Final = "answer"
    RETRY: Final = "retry"
    ACCEPT: Final = "accept"
    CANCEL: Final = "cancel"


class AckStatus:
    """Answers of Shikiru to a command (design §10.1). Shikiru may add more, so these are text."""

    DONE: Final = "done"
    REJECTED: Final = "rejected"
    QUEUED: Final = "queued"


class Ack(_ShikiruPayload):
    """Shikiru's acknowledgement of a command; `id` is the command id the events carry."""

    id: int
    status: str
    result: dict[str, Any] | None
    message: str | None

    @property
    def task_id(self) -> str | None:
        """The task a submit created or will create; None for any other command."""
        value = (self.result or {}).get("task_id")
        return value if isinstance(value, str) else None


class EscalationRecord(_ShikiruPayload):
    id: str
    task_id: str
    reason: str
    headline: str
    status: str
    resolution: str | None


class AgentOption(_ShikiruPayload):
    label: str
    consequences: str
    recommended: bool
    needs_input: bool


class EscalationResolution(_ShikiruPayload):
    kind: str
    consequences: str
    recommended: bool


class EscalationView(_ShikiruPayload):
    """An escalation with the decision fields Shikiru parses from its packet (design §11, S1)."""

    escalation: EscalationRecord
    packet: str | None
    question: str | None
    agent_options: list[AgentOption]
    resolutions: list[EscalationResolution]


class TaskStatus:
    """Task statuses the Operator acts on. Shikiru may add more, so these are text."""

    QUEUED: Final = "queued"
    RUNNING: Final = "running"
    INTEGRATING: Final = "integrating"
    ESCALATED: Final = "escalated"
    DONE: Final = "done"
    CANCELLED: Final = "cancelled"
    ACTIVE: Final = frozenset({QUEUED, RUNNING, INTEGRATING, ESCALATED})


class ShikiruTask(_ShikiruPayload):
    id: str
    parent_id: str | None
    status: str
    title: str


class TaskDetail(_ShikiruPayload):
    task: ShikiruTask


class ShikiruClient(Protocol):
    """One project's Shikiru, reached through its MCP link (design §11).

    Every verb raises ShikiruLinkDownError while the link is down, ShikiruRejectedError when
    Shikiru answers with a tool error, and ShikiruProtocolError for a result it cannot read. A
    command Shikiru acknowledges as `rejected` raises ShikiruRejectedError with Shikiru's message;
    `done` and `queued` come back as the Ack.
    """

    async def wait_for_events(self, since_id: int, timeout_s: float) -> list[ShikiruEvent]: ...

    async def status(self) -> EngineStatus: ...

    async def list_escalations(self) -> list[EscalationView]: ...

    async def get_escalation(self, escalation_id: str) -> EscalationView | None: ...

    async def list_tasks(self, statuses: Sequence[str] | None = None) -> list[ShikiruTask]: ...

    async def get_task(self, task_id: str) -> TaskDetail | None: ...

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack: ...

    async def submit_task(self, request: str) -> Ack: ...

    async def start_engine(self) -> None:
        """Run the project's `shikiru start`; the caller confirms with `status().engine_up`.

        Raises ShikiruRejectedError when the command exits nonzero and CommandError when it
        cannot be run to its end.
        """


class EventJournal(Protocol):
    """What an event source keeps in the journal (design §4, §9)."""

    def last_event_id(self, project: str) -> int | None:
        """Return the project's cursor, or None until its first sync is recorded."""
        ...

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
        """Apply the changes in order, move the cursor, save the respected stop; all or nothing."""
        ...


class LifecycleJournal(Protocol):
    """What the liveness source reads and saves in the journal (design §4, §9)."""

    def state(self, project: str) -> LifecycleState | None:
        """Return the project's cursor facts, or None until its first sync is recorded."""
        ...

    def respect_stop(self, project: str, event_id: int) -> None:
        """Remember a clean stop that was respected, so it never triggers a restart."""
        ...


class SignalLog(Protocol):
    """Where a source opens and closes its signals; opening a known id changes nothing."""

    def open(self, signal: Signal, now: datetime) -> None: ...

    def has_open(self, project: str, kind: Kind, reason: str | None = None) -> bool: ...

    def close(self, closing: Closing, now: datetime) -> None: ...


class ActionLog(Protocol):
    """The automatic actions the loop guard counts and records (design §6, §9)."""

    def times(
        self, kind: str, project: str, cause: Cause | None, since: datetime | None
    ) -> list[datetime]:
        """Return when ops of the kind were journaled after `since` (all if None), oldest first.

        With a cause only the ops of that task and reason count; without one, the whole project.
        """
        ...

    def record(self, op: AutoOp, now: datetime) -> int:
        """Journal the op before it happens and return its id; the identity must be new."""
        ...


class ScheduleLog(Protocol):
    """What the Scheduler reads and writes in the journal (design §6, §9)."""

    def due_parked(self, now: datetime) -> list[Parked]: ...

    def claim(self, number: int, now: datetime) -> bool:
        """Take a parked signal for dispatch; exactly one caller gets True."""
        ...

    def awaiting_answer(self) -> list[Awaiting]: ...

    def schedule_reminder(
        self, awaiting: Awaiting, occurrence: int, now: datetime
    ) -> list[Outgoing]:
        """Journal one reminder per signal and occurrence; an occurrence seen before gives none."""
        ...

    def delivered(self, outgoing: Outgoing, message_ref: str) -> None: ...


class RoutingLog(Protocol):
    """What the Router writes in the journal about one signal's handling (design §6, §9).

    Every move is conditional on the state the Router expects, so a signal is handled once.
    """

    def begin(
        self,
        number: int,
        classification: Classification | None,
        handler: str | None,
        now: datetime,
    ) -> bool:
        """Take a new signal for handling and store its classification and handler.

        False means the signal is no longer new. The classification is None when classifying failed,
        and the handler is None when no handler is registered for the category.
        """
        ...

    def suggestion(self, number: int) -> str:
        """Return the note stored with the signal's classification, or an empty text."""
        ...

    def requeue(self, number: int, now: datetime) -> None:
        """Give a signal back to the dispatcher, because its forward could not be delivered."""
        ...

    def resolve(self, number: int, note: str, now: datetime) -> None: ...

    def forwarded(self, number: int, note: str, now: datetime) -> None: ...

    def park(self, number: int, wake_at: datetime, note: str, now: datetime) -> None: ...


class DispatchLog(Protocol):
    """What the Dispatcher reads in the journal (design §10)."""

    def recover(self, now: datetime) -> int:
        """Put every signal left in `handling` back to `new`; return how many."""
        ...

    def new_signals(self) -> list[tuple[int, Signal]]:
        """Return the signals in state `new` with their numbers, oldest first."""
        ...


class Forwarder(Protocol):
    """The product fallback: puts a signal in front of the PO.

    `note` says what was tried and why it stopped. `suggestion` is the note of the classification,
    the Reasoner's suggestion or its failure, which is model-written and shown as such.
    """

    async def forward(self, signal: Signal, note: str, suggestion: str) -> None: ...


class WakeTarget(Protocol):
    """Where a woken signal goes: the Router runs the handler that parked it."""

    async def wake(self, parked: Parked) -> None: ...


class LivenessJournal(LifecycleJournal, SignalLog, Protocol):
    """Everything the liveness source needs of the journal."""


class Channel(Protocol):
    """The PO's chat (design §7), Telegram first.

    `send` returns the reference the platform gives the message, and `edit` takes it back. The
    inbound stream of taps, replies and commands arrives with the inbound adapter.
    """

    async def send(self, destination: str, text: str) -> str: ...

    async def edit(self, message_ref: str, text: str) -> None: ...


class Reasoner(Protocol):
    """The Reasoner's one job, `assess_blocker` (design §8). Its ladder sits behind this interface.

    Raises ReasonerError when no rung gives a usable answer.
    """

    async def assess_blocker(self, signal: Signal) -> Assessment: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs one stored argument list on this host, for at most `timeout_s` (design §6.3, §12).

    The list runs as given: no shell, and nothing a model wrote. Raises CommandError when the
    command cannot be run to its end.
    """

    async def run(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult: ...


class Clock(Protocol):
    """The only source of time for the core (design §12). Every time it gives is timezone-aware."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class ShikiruError(Exception):
    """Base of the Shikiru errors that cross a port; the subclass says what went wrong."""


class ShikiruLinkDownError(ShikiruError):
    """The MCP link to a project is down, or Shikiru does not answer on it."""


class ShikiruRejectedError(ShikiruError):
    """Shikiru refused a command; its own message says why."""


class ShikiruProtocolError(ShikiruError):
    """Shikiru answered in a shape the Operator does not understand."""


class ChannelError(Exception):
    """A message could not be sent or edited."""


class ReasonerError(Exception):
    """No rung of the Reasoner's ladder gave a usable answer."""


class CommandError(Exception):
    """A host command could not be run to its end: it did not start, or it timed out."""


class CompositionError(Exception):
    """An instance cannot be wired, because its journal cannot be opened or used at startup."""
