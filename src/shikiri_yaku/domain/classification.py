"""Categories, verdicts and the classification of a signal (design §5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NewType, Protocol

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


@dataclass(frozen=True, slots=True)
class Assessment:
    """The Reasoner's answer for one signal: its verdict and the one-line suggestion for the PO."""

    verdict: Verdict
    note: str


type AssessFn = Callable[[Signal], Awaitable[Assessment]]


@dataclass(frozen=True, slots=True)
class Classification:
    """A signal's category and who set it; `note` and `verdict` are set only by the Reasoner."""

    category: Category
    source: Source
    note: str = ""
    verdict: Verdict | None = None


class Classifier(Protocol):
    async def classify(self, signal: Signal) -> Classification: ...
