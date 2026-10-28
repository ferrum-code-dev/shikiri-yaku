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

    from shikiri_yaku.domain.classification import Assessment
    from shikiri_yaku.domain.signal import Signal

__all__ = [
    "Ack",
    "AckStatus",
    "AgentOption",
    "Channel",
    "ChannelError",
    "Classifier",
    "Clock",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "CompositionError",
    "EngineStatus",
    "EscalationRecord",
    "EscalationResolution",
    "EscalationStatus",
    "EscalationView",
    "QuotaWindow",
    "Reasoner",
    "ReasonerError",
    "ResolutionKind",
    "ShikiruClient",
    "ShikiruError",
    "ShikiruEvent",
    "ShikiruLinkDownError",
    "ShikiruProtocolError",
    "ShikiruRejectedError",
    "ShikiruTask",
    "TaskDetail",
    "TaskStatus",
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
