"""The ports: what the core asks of the outside world (design §12).

The core and the extensions depend on these protocols only. Adapters implement them, and
bootstrap.py is the one place that chooses an implementation. A port fails with one of the typed
exceptions below; only the Router turns such a failure into a Forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 -- pydantic reads the annotations at runtime
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, PositiveInt

from shikiri_yaku.domain.classification import Classifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.classification import Assessment, Classification
    from shikiri_yaku.domain.inbound import Batch, Inbound
    from shikiri_yaku.domain.loop_guard import AutoOp, Cause
    from shikiri_yaku.domain.outbound import Purpose
    from shikiri_yaku.domain.schedule import Awaiting, Destination, Parked
    from shikiri_yaku.domain.signal import Closing, Kind, Lifecycle, LifecycleState, Signal

__all__ = [
    "CALLBACK_DATA_LIMIT",
    "MESSAGE_TEXT_LIMIT",
    "Ack",
    "AckStatus",
    "ActionLog",
    "AgentOption",
    "Button",
    "Channel",
    "ChannelError",
    "ChannelRefusedError",
    "Choice",
    "Classifier",
    "Clock",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "CompositionError",
    "Copies",
    "DispatchLog",
    "EngineStatus",
    "EscalationRecord",
    "EscalationResolution",
    "EscalationStatus",
    "EscalationView",
    "EventJournal",
    "EventKinds",
    "Forwarder",
    "InboundLog",
    "LifecycleJournal",
    "LivenessJournal",
    "Message",
    "Outbox",
    "Pending",
    "QuotaWindow",
    "Reasoner",
    "ReasonerError",
    "ReplyLog",
    "ReplyOrigin",
    "Reporter",
    "ResolutionKind",
    "ResolvePayload",
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
    "Updates",
    "WakeTarget",
    "escalation_choices",
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
        self, awaiting: Awaiting, occurrence: int, message: Message, now: datetime
    ) -> None:
        """Queue one reminder per signal and occurrence; an occurrence seen before queues none."""
        ...


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

    `number` is the signal's journal number, its reference `S<number>`. `note` says what was tried
    and why it stopped. `suggestion` is the note of the classification, the Reasoner's suggestion
    or its failure, which is model-written and shown as such.
    """

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None: ...


class WakeTarget(Protocol):
    """Where a woken signal goes: the Router runs the handler that parked it."""

    async def wake(self, parked: Parked) -> None: ...


class LivenessJournal(LifecycleJournal, SignalLog, Protocol):
    """Everything the liveness source needs of the journal."""


# keep: Telegram's limits (design §7), named here because the Channel is the only user of them
MESSAGE_TEXT_LIMIT: Final = 4096
CALLBACK_DATA_LIMIT: Final = 64


@dataclass(frozen=True, slots=True)
class Button:
    """An inline button. Its `data` comes back in the tap, so it carries numbers (design §7)."""

    label: str
    data: str


@dataclass(frozen=True, slots=True)
class Message:
    """What the Channel shows: a text and the buttons under it."""

    text: str
    buttons: tuple[Button, ...] = ()

    def texts(self) -> tuple[str, ...]:
        """Every text the message puts in front of the PO or sends back in a tap."""
        labels = [button.label for button in self.buttons]
        data = [button.data for button in self.buttons]
        return (self.text, *labels, *data)


class Channel(Protocol):
    """The PO's chat (design §7), Telegram first.

    `send` returns the reference the platform gives the message, and `edit` takes it back. A
    message the platform refuses for good raises ChannelRefusedError; any other failure raises
    ChannelError and is tried again. What the PO sends comes through `Updates`.
    """

    async def send(self, destination: str, message: Message) -> str: ...

    async def edit(self, message_ref: str, message: Message) -> None: ...


class Updates(Protocol):
    """Poll from `offset` for up to `wait_s`; transport or protocol failures raise ChannelError."""

    async def fetch(self, offset: int, wait_s: float) -> Batch: ...


class InboundLog(Protocol):
    """Inbound items in the journal, kept until Replies has handled them (design §7, §9)."""

    def next_offset(self, channel: str) -> int:
        """Return the offset to poll from: past the last batch that was journaled, 0 at first."""
        ...

    def record_batch(
        self, channel: str, items: Sequence[Inbound], next_offset: int, now: datetime
    ) -> None:
        """Store the new items and move the offset, in one transaction.

        An item whose update id is known is not stored again. Text that carries a secret form is
        stored as refused, without its text.
        """
        ...

    def pending(self) -> list[Inbound]:
        """Return the stored items not yet handled, oldest first."""
        ...

    def mark_handled(self, update_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Pending:
    """A recorded message the sender has not yet seen confirmed."""

    message_id: int
    destination: Destination
    message: Message


class Outbox(Protocol):
    """Every message to the PO is recorded here before it is sent (design §7, §10).

    One sender drains it: a restart sends what was recorded and not confirmed, and a confirmed
    message is never offered again. A recorded body carries its signal reference (`S41`), so a
    resend after an unconfirmed send is recognizable.
    """

    def queue(
        self,
        number: int,
        purpose: Purpose,
        destinations: Sequence[Destination],
        message: Message,
        *,
        key: str,
        now: datetime,
    ) -> None:
        """Record the message once per key and destination; a key seen before records nothing."""
        ...

    def pending(self) -> list[Pending]: ...

    def confirm(self, message_id: int, message_ref: str) -> None: ...

    def refuse(self, message_id: int, reason: str, *, now: datetime) -> None:
        """Record a refusal for good: the body is dropped and only the reason stays.

        A refused message is never offered again. The PO gets a notice of it, with no content.
        """
        ...


class Copies(Protocol):
    """The journal's record of a sent message, which an edit of it can be refused for (design §7).

    Each call is journaled before the platform is asked. False means the journal does not know a
    sent copy at that reference: then nothing is journaled and nothing is deleted.
    """

    def record_refused_edit(self, message_ref: str, reason: str, *, now: datetime) -> None:
        """Keep the reason on the copy and queue the PO a notice; the copy itself stays."""
        ...

    def record_deletion(self, message_ref: str, reason: str, *, now: datetime) -> bool:
        """Drop the copy's body, mark its deletion as attempted, and queue the PO a notice."""
        ...

    def confirm_deletion(self, message_ref: str) -> None: ...


class Reporter(Protocol):
    """Where a fault the journal cannot keep is reported: one line of metadata, never content."""

    def report(self, line: str) -> None: ...


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
    """A message could not be sent or edited; it may be tried again."""


class ChannelRefusedError(Exception):
    """The platform will never take this message, however often it is sent.

    The text is the reason. It is not a ChannelError, so a sender never retries it.
    """


class ReasonerError(Exception):
    """No rung of the Reasoner's ladder gave a usable answer."""


class CommandError(Exception):
    """A host command could not be run to its end: it did not start, or it timed out."""


class CompositionError(Exception):
    """An instance cannot be wired, because its journal cannot be opened or used at startup."""


@dataclass(frozen=True, slots=True)
class Choice:
    number: int
    value: AgentOption | EscalationResolution


def escalation_choices(view: EscalationView) -> list[Choice]:
    """Assign numbers to options then resolutions, retaining gaps for unsupported kinds."""
    options = [Choice(number, value) for number, value in enumerate(view.agent_options, start=1)]
    known = {
        ResolutionKind.ANSWER,
        ResolutionKind.RETRY,
        ResolutionKind.ACCEPT,
        ResolutionKind.CANCEL,
    }
    return options + [
        Choice(number, value)
        for number, value in enumerate(view.resolutions, start=len(options) + 1)
        if value.kind in known
    ]


class ResolvePayload(BaseModel):
    """The stored command arguments; issuance reads these rather than rebuilding a choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    number: PositiveInt
    project: str
    task_id: str | None
    reason: str
    escalation_id: str
    kind: str
    text: str | None


@dataclass(frozen=True, slots=True)
class ReplyOrigin:
    number: int
    signal: Signal
    purpose: Purpose
    message: Message
    key: str | None
    destination: Destination


class ReplyLog(Protocol):
    """Origin binding and once-only resolve issuance; issued ops await observation in A12."""

    def origin(self, channel: str, message_ref: str) -> ReplyOrigin | None: ...

    def intend_resolve(self, update_id: int, payload: ResolvePayload, now: datetime) -> int | None:
        """Keep the first payload; a secret is refused with sanitized metadata and returns None."""
        ...

    def approve_resolve(self, op_id: int, number: int, now: datetime) -> bool:
        """Approve a stored accept/cancel for the originating signal before it can be claimed."""
        ...

    def claim_resolve(self, op_id: int, now: datetime) -> ResolvePayload | None:
        """Claim the stored payload once; accept/cancel require durable approval."""
        ...

    def acknowledge_resolve(self, op_id: int, ack: Ack, now: datetime) -> None:
        """Save the acknowledgement and command id without confirming the op."""
        ...

    def acknowledge_notice(self, number: int, now: datetime) -> None:
        """Close an informational signal without issuing a Shikiru resolution."""
        ...
