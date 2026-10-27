"""The ports: what the core asks of the outside world (design §12).

The core and the extensions depend on these protocols only. Adapters implement them, and
bootstrap.py is the one place that chooses an implementation. A port fails with one of the typed
exceptions below; only the Router turns such a failure into a Forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from shikiri_yaku.domain.classification import Classifier

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from shikiri_yaku.domain.classification import Assessment
    from shikiri_yaku.domain.signal import Signal

__all__ = [
    "Channel",
    "ChannelError",
    "Classifier",
    "Clock",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "EngineStatus",
    "Reasoner",
    "ReasonerError",
    "ShikiruClient",
    "ShikiruError",
    "ShikiruLinkDownError",
    "ShikiruProtocolError",
    "ShikiruRejectedError",
]


@dataclass(frozen=True, slots=True)
class EngineStatus:
    engine_up: bool


class ShikiruClient(Protocol):
    """One project's Shikiru, reached through its MCP link (design §11).

    Only `status` is declared here. The other verbs of §11 come with the MCP adapter, which brings
    the result types they need.
    """

    async def status(self) -> EngineStatus: ...


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
