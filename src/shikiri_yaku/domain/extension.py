"""An extension: the rules, handler and actions of one category (design §5, §6, §12)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shikiri_yaku.domain.classification import Category, Verdict
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class Rule:
    """Claims the signals whose reason key is in `reasons`; `None` claims every other signal.

    A rule with `verdicts` applies only when the Reasoner's verdict on the signal is one of them.
    Without a verdict function such a rule never applies.
    """

    reasons: frozenset[str] | None
    verdicts: frozenset[Verdict] = frozenset()


class Handler(Protocol):
    async def handle(self, signal: Signal) -> Outcome: ...


class Action(Protocol):
    reasons: frozenset[str]

    async def run(self, signal: Signal) -> Outcome: ...


@dataclass(frozen=True, slots=True)
class Extension:
    category: Category
    rules: tuple[Rule, ...]
    handler: Handler
    actions: tuple[Action, ...] = ()
