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
from shikiri_yaku.domain.schedule import Destination  # noqa: TC001 -- pydantic reads it at runtime

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.domain.classification import Assessment, Classification
    from shikiri_yaku.domain.inbound import Batch, Inbound
    from shikiri_yaku.domain.loop_guard import AutoOp, Cause, Granted, Refused
    from shikiri_yaku.domain.outbound import Purpose
    from shikiri_yaku.domain.schedule import Awaiting, Parked
    from shikiri_yaku.domain.signal import (
        Closing,
        Kind,
        Lifecycle,
        LifecycleState,
        Resolved,
        Signal,
    )

__all__ = [
    "BLOCK_SEPARATOR",
    "CALLBACK_DATA_LIMIT",
    "MESSAGE_TEXT_LIMIT",
    "Ack",
    "AckStatus",
    "ActionLog",
    "AgentOption",
    "AttemptState",
    "Button",
    "Channel",
    "ChannelError",
    "ChannelProtocolError",
    "ChannelRefusedError",
    "ChannelStartupError",
    "Choice",
    "Classifier",
    "Clock",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "CompletedEscalation",
    "CompositionError",
    "Copies",
    "Destinations",
    "DispatchLog",
    "DoctorCheck",
    "DoctorReport",
    "EngineStatus",
    "EscalationRecord",
    "EscalationResolution",
    "EscalationStatus",
    "EscalationView",
    "EventJournal",
    "EventKinds",
    "Forwarder",
    "Guard",
    "InboundLog",
    "LifecycleJournal",
    "LivenessJournal",
    "Message",
    "Notifier",
    "Outbox",
    "Pending",
    "QuotaWindow",
    "Reasoner",
    "ReasonerError",
    "ReconcileLog",
    "RecordedResolution",
    "ReplyLog",
    "ReplyOrigin",
    "Reporter",
    "Reshower",
    "ResolutionKind",
    "ResolvePayload",
    "RetryAttempt",
    "RetryLog",
    "RoutingLog",
    "ScheduleLog",
    "SecretLog",
    "ShikiruClient",
    "ShikiruError",
    "ShikiruEvent",
    "ShikiruLinkDownError",
    "ShikiruProtocolError",
    "ShikiruRejectedError",
    "ShikiruTask",
    "SignalLog",
    "SignalNumbers",
    "StartAttempt",
    "StartLog",
    "StatsLog",
    "SubmitLog",
    "SubmitOp",
    "SubmitPayload",
    "SubmitStatus",
    "TaskDetail",
    "TaskStatus",
    "Unsettled",
    "UnsettledStart",
    "Updates",
    "WaitAttempt",
    "WaitLog",
    "WakeTarget",
    "escalation_choices",
    "join_blocks",
    "text_room",
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


class DoctorCheck(_ShikiruPayload):
    name: str
    ok: bool
    detail: str


class DoctorReport(_ShikiruPayload):
    checks: list[DoctorCheck]


class ShikiruClient(Protocol):
    """One project's Shikiru, reached through its MCP link (design §11).

    Every verb raises ShikiruLinkDownError while the link is down, ShikiruRejectedError when
    Shikiru answers with a tool error, and ShikiruProtocolError for a result it cannot read. A
    command Shikiru acknowledges as `rejected` raises ShikiruRejectedError with Shikiru's message;
    `done` and `queued` come back as the Ack.
    """

    @property
    def linked(self) -> bool: ...

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

    async def doctor(self) -> list[DoctorCheck]:
        """Return the checks that did not pass; an empty list when all of them passed.

        `shikiru doctor` exits nonzero when a check fails, so only a missing report is an error:
        ShikiruProtocolError. CommandError when the command cannot be run to its end.
        """
        ...


class EventJournal(Protocol):
    """What an event source keeps in the journal (design §4, §9)."""

    def last_event_id(self, project: str) -> int | None:
        """Return the project's cursor, or None until its first sync is recorded."""
        ...

    def record_batch(
        self,
        project: str,
        changes: Sequence[Signal | Closing | Resolved],
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


class SignalNumbers(Protocol):
    """The number the journal gave a signal, which a handler puts in the message it sends."""

    def number_of(self, signal: Signal) -> int | None: ...


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


class Guard(Protocol):
    """The loop guard as the actions see it: an automatic action asks before it runs (design §6)."""

    def retry(self, project: str, task_id: str | None, reason: str) -> Granted | Refused: ...

    def restart(self, project: str) -> Granted | Refused: ...


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
# keep: the blank line that sets each block of a message off from the next (design §6.1)
BLOCK_SEPARATOR: Final = "\n\n"


def join_blocks(*blocks: str) -> str:
    """Join the non-empty blocks of a message, each set off from the next by a blank line."""
    return BLOCK_SEPARATOR.join(block for block in blocks if block)


def text_room(heading: str) -> int:
    """Return how many characters of text fit in a message under this heading, within the limit."""
    overhead = len(heading) + len(BLOCK_SEPARATOR) if heading else 0
    return MESSAGE_TEXT_LIMIT - overhead


@dataclass(frozen=True, slots=True)
class Button:
    """An inline button: a tap sends its `data` back, or a link opens its `url` (design §6.4, §7).

    `data` carries numbers only, so it stays short; a button has exactly one of the two.
    """

    label: str
    data: str = ""
    url: str | None = None

    def __post_init__(self) -> None:
        if bool(self.data) == bool(self.url):
            message = "a button takes exactly one of data or url"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Message:
    """What the Channel shows: a heading, a text and the buttons under it (design §6.1).

    The heading comes first, set off from the text by a blank line. The Channel decides how to show
    the heading; the characters the PO reads, which the limit counts, are `shown()`.

    A message that waits for the PO's typed text (`expects_reply`) has no buttons: the Channel
    opens the reply box in its place.
    """

    text: str
    buttons: tuple[Button, ...] = ()
    heading: str = ""
    expects_reply: bool = False

    def __post_init__(self) -> None:
        if self.expects_reply and self.buttons:
            message = "a message takes buttons or a reply, not both"
            raise ValueError(message)

    def shown(self) -> str:
        """Return the characters the PO reads: the heading, a blank line, then the text."""
        return join_blocks(self.heading, self.text)

    def texts(self) -> tuple[str, ...]:
        """Every text the message puts in front of the PO or sends back in a tap or a link."""
        labels = [button.label for button in self.buttons]
        data = [button.data for button in self.buttons if button.data]
        urls = [button.url for button in self.buttons if button.url]
        return (self.heading, self.text, *labels, *data, *urls)


class Channel(Protocol):
    """The PO's chat (design §7), Telegram first.

    `send` returns the reference the platform gives the message, and `edit` and `delete` take it
    back. A message the platform refuses for good raises ChannelRefusedError; any other failure
    raises ChannelError and is tried again. What the PO sends comes through `Updates`.
    """

    async def send(self, destination: Destination, message: Message) -> str: ...

    async def edit(self, message_ref: str, message: Message) -> None: ...

    async def delete(self, message_ref: str) -> None: ...


class Destinations(Protocol):
    """Where a Channel's messages go, and what an inbound item says of its component (design §13).

    The core asks for destinations by project and kind, and for the component an inbound thread
    implies, and never reads how the channel organises its chats. `prepare` runs once at start-up,
    before anything is sent, and raises ChannelStartupError if the channel cannot be made ready.
    """

    def for_signal(self, project: str, kind: Kind) -> Sequence[Destination]: ...

    def component_of(self, thread: int | None) -> str | None: ...

    async def prepare(self) -> None: ...


class Updates(Protocol):
    """Poll from `offset` for up to `wait_s`.

    A transport failure raises ChannelError; an unreadable reply raises ChannelProtocolError.
    """

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
        number: int | None,
        purpose: Purpose,
        destinations: Sequence[Destination],
        message: Message,
        *,
        key: str,
        now: datetime,
    ) -> None:
        """Record the message once per key and destination; a key seen before records nothing."""
        ...

    def command_reply_queued(self, key: str) -> bool:
        """Return whether this logical command reply and all of its parts were journaled."""
        ...

    def queue_command_reply(
        self,
        destinations: Sequence[Destination],
        parts: Sequence[Message],
        *,
        key: str,
        now: datetime,
    ) -> None:
        """Journal a command reply claim and every bounded part atomically, once per update."""
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


@dataclass(frozen=True, slots=True)
class FinalEdit:
    """A journaled edit of one sent copy to its final state."""

    message_ref: str
    message: Message


class FinalEdits(Protocol):
    """Edits of every copy of a message to its final state, each journaled before the platform.

    `settle` records the final state of all copies of a signal that still show buttons; `to_edit`
    returns the recorded edits not yet confirmed, so a restart finishes them. An edit the platform
    refuses for good is given up and never offered again.
    """

    def unsettled(self) -> list[tuple[int, str]]:
        """Return the signals whose resolution is not yet shown on every copy, with its line."""
        ...

    def settle(self, number: int, line: str) -> None:
        """Record, for each sent copy with buttons, its text with the line added and no buttons."""
        ...

    def to_edit(self) -> list[FinalEdit]: ...

    def confirm_edit(self, message_ref: str) -> None: ...

    def give_up(self, message_ref: str, reason: str) -> None: ...


class SecretLog(Protocol):
    """Inbound messages that carried a secret, and the attempt to delete each (design §7).

    The attempt is journaled before the platform is asked. An attempt the platform refuses is never
    repeated; one that fails for a transient reason goes back to be tried again.
    """

    def to_delete(self) -> list[Inbound]:
        """Return the messages still to delete, oldest first."""
        ...

    def record_deletion(self, update_id: int) -> None: ...

    def defer_deletion(self, update_id: int) -> None: ...

    def confirm_deletion(self, update_id: int) -> None: ...


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


class ChannelProtocolError(ChannelError):
    """The platform answered in a shape the Channel does not read; retrying alone will not help."""


class ChannelRefusedError(Exception):
    """The platform will never take this message, however often it is sent.

    The text is the reason. It is not a ChannelError, so a sender never retries it.
    """


class ChannelStartupError(Exception):
    """A channel cannot be made ready at start-up, and the service must not run without it.

    The text is the reason and says what the PO has to inspect. The service stops on it.
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
    """The stored command arguments; issuance reads these rather than rebuilding a choice.

    `label` is what the PO chose, as the Operator says it again to a later answer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    number: PositiveInt
    project: str
    task_id: str | None
    reason: str
    escalation_id: str
    kind: str
    text: str | None
    label: str


@dataclass(frozen=True, slots=True)
class ReplyOrigin:
    number: int
    signal: Signal
    purpose: Purpose
    message: Message
    key: str | None
    destination: Destination


@dataclass(frozen=True, slots=True)
class RecordedResolution:
    """A resolution Shikiru confirmed: the label the PO chose and when that was journaled."""

    label: str
    at: datetime


class ReplyLog(Protocol):
    """Origin binding and once-only resolve issuance; issued ops await observation in A12."""

    def origin(self, channel: str, message_ref: str) -> ReplyOrigin | None: ...

    def sent_message(self, destination: Destination, message_ref: str) -> Message | None:
        """Return the body of the message sent to the destination, or None if none is journaled."""
        ...

    def recorded_resolution(self, number: int) -> RecordedResolution | None:
        """Return the resolution Shikiru confirmed for the signal, or None when there is none."""
        ...

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

    def reject_resolve(self, op_id: int, now: datetime) -> bool:
        """Fail an issued op that Shikiru refused; return whether it was still issued."""
        ...

    def acknowledge_notice(self, number: int, notice_key: str | None, now: datetime) -> None:
        """Silence the reminders of a silencing notice, or close the signal of any other notice."""
        ...


@dataclass(frozen=True, slots=True)
class Unsettled:
    """An issued resolve no event has confirmed; `since` is when it last changed."""

    op_id: int
    payload: ResolvePayload
    signal: Signal
    ack: Ack | None
    since: datetime


class ReconcileLog(Protocol):
    """The issued resolves to reconcile by observation (design §10.1)."""

    def unsettled(self) -> list[Unsettled]: ...

    def confirm(self, op_id: int, now: datetime) -> bool:
        """Confirm an issued op whose escalation is seen resolved as asked."""
        ...

    def fail(self, op_id: int, now: datetime, *, question_shown: bool = False) -> bool:
        """Fail an issued op; a re-shown question also settles signal replay to forwarded."""
        ...


class RetryLog(Protocol):
    """Automatic retries, journaled as resolve ops so the events and the reconciler settle them."""

    def existing(self, signal: Signal) -> RetryAttempt | None:
        """Find the signal-keyed retry, if this signal already caused one."""
        ...

    def issue(self, signal: Signal, escalation_id: str, now: datetime) -> int | None:
        """Journal the retry as issued before Shikiru is called; None if already journaled."""
        ...

    def acknowledge(self, op_id: int, ack: Ack, now: datetime) -> None:
        """Save the acknowledgement and command id of a retry Shikiru accepted."""
        ...

    def reject(self, op_id: int, now: datetime) -> None:
        """Fail a retry Shikiru refused."""
        ...


class AttemptState:
    """States of an automatic attempt, a retry or an engine start, as text (design §6.2, §9)."""

    ISSUED: Final = "issued"
    CONFIRMED: Final = "confirmed"
    FAILED: Final = "failed"


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """The journal state of a retry already keyed to a signal."""

    state: str


class Notifier(Protocol):
    """Queues an informational message about a signal, once per key (design §7).

    A notice is not a question, so it has no signal number. The key names what the notice is
    about, so a handler that runs again sends nothing twice.
    """

    def tell(self, signal: Signal, text: str, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class WaitAttempt:
    """The quota wait journaled for a signal: when it is due (design §6.2)."""

    wake_at: datetime


class WaitLog(Protocol):
    """Quota waits, journaled as wait ops so the op, not memory, says when a signal is due."""

    def existing(self, signal: Signal) -> WaitAttempt | None:
        """Find the wait journaled for this signal, if it parked before."""
        ...

    def record(self, signal: Signal, wake_at: datetime, now: datetime) -> None:
        """Journal the wait before the signal parks; recording it again changes nothing."""
        ...

    def finish(self, signal: Signal, now: datetime) -> None:
        """Mark the wait over once its wake-up is being handled."""
        ...


@dataclass(frozen=True, slots=True)
class StartAttempt:
    """The journal state of an engine start already keyed to a signal."""

    state: str


@dataclass(frozen=True, slots=True)
class UnsettledStart:
    """An engine start issued and not settled; `since` is when it was issued."""

    signal: Signal
    since: datetime


class StartLog(Protocol):
    """Engine starts, journaled as start ops before Shikiru's `start` runs (design §6.2, §10)."""

    def existing(self, signal: Signal) -> StartAttempt | None:
        """Find the start already journaled for this signal, if any."""
        ...

    def issue(self, signal: Signal, now: datetime) -> None:
        """Journal the start as issued before Shikiru is called; a start already journaled stays."""
        ...

    def confirm(self, signal: Signal, now: datetime) -> None:
        """Mark a start confirmed once the engine is seen up."""
        ...

    def fail(self, signal: Signal, now: datetime) -> None:
        """Mark a start failed once the engine did not come up in time."""
        ...

    def unsettled(self) -> list[UnsettledStart]:
        """Return the starts issued and not yet confirmed or failed."""
        ...


class SubmitStatus:
    """States of a submit op (design §9, §10.1), as text: ports cannot import the journal's enum."""

    INTENDED: Final = "intended"
    ISSUED: Final = "issued"
    CONFIRMED: Final = "confirmed"
    FAILED: Final = "failed"
    UNKNOWN: Final = "unknown"


class SubmitPayload(BaseModel):
    """What a submit op sends: the component whose Shikiru takes it, and the request (§7).

    `destination` is the chat the outcome is reported in. `retry_of` is the unknown submit that a
    replacement consumed; it is None for a goal the PO sent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    component: str
    request: str
    destination: Destination
    retry_of: int | None = None


@dataclass(frozen=True, slots=True)
class SubmitOp:
    """A submit op as the journal keeps it; `task_id` is set once Shikiru has acknowledged it."""

    id: int
    payload: SubmitPayload
    status: str
    task_id: str | None


class SubmitLog(Protocol):
    """Submit ops in the journal (design §7, §9). A submit is sent only from an intended op."""

    def intend(self, update_id: int, payload: SubmitPayload, now: datetime) -> SubmitOp:
        """Journal the submit once per Telegram update id, and return the op as stored."""
        ...

    def get(self, op_id: int) -> SubmitOp | None: ...

    def claim(self, op_id: int, now: datetime) -> bool:
        """Move an intended op to issued before the call; exactly one caller wins."""
        ...

    def confirm(self, op_id: int, ack: Ack, now: datetime) -> None:
        """Confirm an issued op with the acknowledgement that carries its task id."""
        ...

    def fail(self, op_id: int, now: datetime) -> None:
        """Fail an issued op that Shikiru rejected."""
        ...

    def uncertain(self, op_id: int, now: datetime) -> None:
        """Mark an issued op unknown: it may have reached Shikiru, so it is never sent again."""
        ...

    def retry(self, prompt_id: int, update_id: int, now: datetime) -> SubmitOp | None:
        """Consume an unknown submit and journal its replacement under the tap's update id.

        The prompt keeps its unknown outcome. A replayed tap gets the replacement it made; None
        when the prompt is not unknown or another tap consumed it first.
        """
        ...

    def unfinished(self) -> list[SubmitOp]:
        """Return the submit ops that are neither confirmed nor failed, oldest first.

        An unknown op that a retry consumed is left out: its replacement carries the question.
        """
        ...


class Reshower(Protocol):
    """Shows a signal's question again as a new message, once per key, while Shikiru has it open."""

    async def reshow(self, number: int, signal: Signal, note: str, *, key: str) -> bool:
        """Return whether the question was queued; it is not when Shikiru no longer has it open."""
        ...


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """An accept or cancel not issued yet, intended or approved; `kind` is what it would issue."""

    op_id: int
    number: int
    project: str
    escalation_id: str
    kind: str


class OpenLog(Protocol):
    """What /open shows again: the forwarded questions and the approvals awaiting Confirm."""

    def forwarded(self) -> list[tuple[int, Signal]]:
        """Return the forwarded signals with an escalation and their numbers, oldest first."""
        ...

    def pending_approvals(self) -> list[PendingApproval]:
        """Return the accept and cancel ops intended or approved but not issued, oldest first."""
        ...


@dataclass(frozen=True, slots=True)
class CompletedEscalation:
    """An escalation signal that reached resolved or closed, with the journal's own times.

    `first_queued_at` is its earliest outbound question or approval, or None if none was recorded.
    """

    created_at: datetime
    final_at: datetime
    first_queued_at: datetime | None


class StatsLog(Protocol):
    """What `stats` reads of the journal (design §15).

    Counts are per category, reason, verdict or current op state; a None category or verdict is an
    absent one. The states are not outcomes.
    """

    def completed_escalations(self) -> list[CompletedEscalation]: ...
    def handled_without_po(self) -> Mapping[str | None, int]: ...
    def escalation_reasons(self) -> Mapping[str, int]: ...
    def escalation_verdicts(self) -> Mapping[str | None, int]: ...
    def install_states(self) -> Mapping[str, int]: ...
