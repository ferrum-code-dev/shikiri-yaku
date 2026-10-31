"""Categories, verdicts and the classification of a signal (design §5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NewType, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from shikiri_yaku.domain.signal import Signal

Category = NewType("Category", str)


class Verdict(StrEnum):
    HOST_TOOL = "host_tool"
    PROJECT_DEPENDENCY = "project_dependency"
    OTHER = "other"


class Source(StrEnum):
    RULE = "rule"
    REASONER = "reasoner"


class InstallProposal(BaseModel):
    """The Reasoner's typed proposal, kept with its classification for the Install action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str | None
    manager: str | None
    package: str | None
    why: str | None


@dataclass(frozen=True, slots=True)
class CallRecord:
    """What one Reasoner call reported about itself; a field is None when its CLI reported none."""

    duration_ms: int | None
    cost_usd: float | None
    usage: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class Assessment:
    """The Reasoner's answer for one signal: its verdict and the one-line suggestion for the PO.

    `tool`, `manager`, `package` and `why` are the model's words as it gave them, None when it
    named none. Code checks the names before any of them is used (design §6.3). `call` is the
    usage and timing of the call, None when its CLI reported none (design §8).
    """

    verdict: Verdict
    note: str
    tool: str | None = None
    manager: str | None = None
    package: str | None = None
    why: str | None = None
    call: CallRecord | None = None


type AssessFn = Callable[[Signal], Awaitable[Assessment]]


@dataclass(frozen=True, slots=True)
class Classification:
    """A signal's category and who set it; the Reasoner alone sets `note`, `verdict` and `call`."""

    category: Category
    source: Source
    note: str = ""
    verdict: Verdict | None = None
    call: CallRecord | None = None
    install_proposal: InstallProposal | None = None


class Classifier(Protocol):
    async def classify(self, signal: Signal) -> Classification: ...
