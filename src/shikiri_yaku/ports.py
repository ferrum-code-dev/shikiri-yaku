"""The ports: what the core asks of the outside world (design §12).

The core and the extensions depend on these protocols only. Adapters implement them, and
bootstrap.py is the one place that chooses an implementation. A port fails with one of the typed
exceptions below; only the Router turns such a failure into a Forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 -- pydantic reads the annotations at runtime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shikiri_yaku.domain.classification import Classifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.classification import Assessment
    from shikiri_yaku.domain.signal import Signal

__all__ = [
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
    "EscalationView",
    "QuotaWindow",
    "Reasoner",
    "ReasonerError",
    "ShikiruClient",
    "ShikiruError",
    "ShikiruEvent",
    "ShikiruLinkDownError",
    "ShikiruProtocolError",
    "ShikiruRejectedError",
    "ShikiruTask",
    "TaskDetail",
    "TaskRecord",
    "TaskStatus",
]


type _Unread = object | None


class _ShikiruPayload(BaseModel):
    """A payload of Shikiru's MCP server (design §11).

    Fields are forbidden beyond those declared, so a change in Shikiru's views surfaces as a
    ShikiruProtocolError. The fields the Operator does not use are declared as `_Unread` and never
    looked at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class QuotaWindow(_ShikiruPayload):
    name: str
    used_pct: float
    resets_at: datetime | None
    observed_at: _Unread = None


class EngineStatus(_ShikiruPayload):
    engine_up: bool
    quotas: dict[str, list[QuotaWindow]] = Field(default_factory=dict)
    project: _Unread = None
    slots: _Unread = None
    active_tasks: _Unread = None
    queued_tasks: _Unread = None
    escalations: _Unread = None
    usage: _Unread = None
    faults: _Unread = None
    oldest_escalation_age_seconds: _Unread = None
    run_directories: _Unread = None
    engine_output: _Unread = None
    task: _Unread = None


class ShikiruEvent(_ShikiruPayload):
    """One event of a project's log; `kind` stays text because Shikiru adds kinds."""

    id: int
    ts: datetime
    task_id: str | None = None
    kind: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class EscalationRecord(_ShikiruPayload):
    id: str
    task_id: str
    reason: str
    headline: str
    status: Literal["open", "resolved"]
    resolution: str | None = None
    step: _Unread = None
    packet_path: _Unread = None
    options: _Unread = None
    created_at: _Unread = None
    resolved_at: _Unread = None


class AgentOption(_ShikiruPayload):
    label: str
    consequences: str
    recommended: bool
    needs_input: bool


class EscalationResolution(_ShikiruPayload):
    kind: Literal["answer", "retry", "accept", "cancel"]
    consequences: str
    recommended: bool


class EscalationView(_ShikiruPayload):
    """An escalation with the decision fields Shikiru parses from its packet (design §11, S1)."""

    escalation: EscalationRecord
    packet: str | None
    packet_error: _Unread = None
    question: str | None = None
    agent_options: list[AgentOption] = Field(default_factory=list)
    resolutions: list[EscalationResolution] = Field(default_factory=list)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INTEGRATING = "integrating"
    ESCALATED = "escalated"
    DONE = "done"
    CANCELLED = "cancelled"


class ShikiruTask(_ShikiruPayload):
    id: str
    parent_id: str | None = None
    status: TaskStatus
    title: str
    step: _Unread = None
    priority: _Unread = None
    wait_reason: _Unread = None
    holds: _Unread = None
    slot: _Unread = None
    summary: _Unread = None


class TaskRecord(ShikiruTask):
    """The task inside `get_task`, which carries all of Shikiru's task fields."""

    seq: _Unread = None
    origin: _Unread = None
    request: _Unread = None
    mode: _Unread = None
    complexity: _Unread = None
    depends_on: _Unread = None
    status_reason: _Unread = None
    iteration: _Unread = None
    counters: _Unread = None
    rung_index: _Unread = None
    overrides: _Unread = None
    repo: _Unread = None
    window_id: _Unread = None
    panes: _Unread = None
    requeued_at: _Unread = None
    created_at: _Unread = None
    started_at: _Unread = None
    finished_at: _Unread = None


class TaskDetail(_ShikiruPayload):
    task: TaskRecord
    events: _Unread = None
    reports: _Unread = None
    recorded_facts: _Unread = None
    facts_turn: _Unread = None
    evidence_errors: _Unread = None
    branches: _Unread = None
    sessions: _Unread = None


class ShikiruClient(Protocol):
    """One project's Shikiru, reached through its MCP link (design §11).

    Every verb raises ShikiruLinkDownError while the link is down, ShikiruRejectedError when
    Shikiru answers with a tool error, and ShikiruProtocolError for a result it cannot read. The
    commands of §10.1 arrive with the next task.
    """

    async def wait_for_events(self, since_id: int, timeout_s: float) -> list[ShikiruEvent]: ...

    async def status(self) -> EngineStatus: ...

    async def list_escalations(self) -> list[EscalationView]: ...

    async def get_escalation(self, escalation_id: str) -> EscalationView | None: ...

    async def list_tasks(
        self, statuses: Sequence[TaskStatus] | None = None
    ) -> list[ShikiruTask]: ...

    async def get_task(self, task_id: str) -> TaskDetail | None: ...


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
