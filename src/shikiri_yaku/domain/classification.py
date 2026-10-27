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


type VerdictFn = Callable[[Signal], Awaitable[Verdict]]


@dataclass(frozen=True, slots=True)
class Classification:
    category: Category
    source: Source
    verdict: Verdict | None


class Classifier(Protocol):
    async def classify(self, signal: Signal) -> Classification: ...
