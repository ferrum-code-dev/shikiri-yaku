"""An extension: the rules, handler and actions of one category."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from shikiri_yaku.domain.classification import Category, Verdict
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Kind, Signal


@dataclass(frozen=True, slots=True)
class Rule:
    """Claims escalations by `reasons` and the other signals by `kinds`.

    `catch_all` claims every signal no other rule claims; it names nothing and comes last.
    A rule with `verdicts` applies only when the Reasoner's verdict on the signal is one of them.
    Without an assessment function such a rule never applies.
    """

    reasons: frozenset[str] = frozenset()
    kinds: frozenset[Kind] = frozenset()
    verdicts: frozenset[Verdict] = frozenset()
    catch_all: bool = False


class Handler(Protocol):
    async def handle(self, signal: Signal) -> Outcome: ...


class Action(Protocol):
    reasons: frozenset[str]

    async def run(self, signal: Signal) -> Outcome: ...


def no_standing(signal: Signal) -> bool:
    """Refuse standing: a failure of any signal is forwarded as information, the default."""
    del signal
    return False


@dataclass(frozen=True, slots=True)
class LegacyCopy:
    """A notice sent before the standing flag existed, which its category recognizes.

    Its notice key is `key_prefix` and the signal's number, and the signal has one of `kinds`.
    Some legacy notices also require the signal's pull request to be recorded.
    """

    key_prefix: str
    kinds: frozenset[Kind]
    requires_pull_request: bool = False


@dataclass(frozen=True, slots=True)
class Extension:
    """`stands_on_failure` says whether a failure of one of its signals keeps the signal open.

    `legacy_copies` names the notices an upgrade marks standing when they carry the button.
    """

    category: Category
    rules: tuple[Rule, ...]
    handler: Handler
    actions: tuple[Action, ...] = ()
    stands_on_failure: Callable[[Signal], bool] = no_standing
    legacy_copies: tuple[LegacyCopy, ...] = ()
