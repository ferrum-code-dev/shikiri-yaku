"""The rule classifier: the rules of the registered extensions, in order (design §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.classification import Classification, Source
from shikiri_yaku.domain.signal import Kind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.classification import AssessFn, Assessment, Category
    from shikiri_yaku.domain.extension import Extension, Rule
    from shikiri_yaku.domain.signal import Signal

type _Entry = tuple[Category, Rule]


class RegistrationError(Exception):
    """Extensions that cannot be used together; raised when the classifier is built."""


class RuleClassifier:
    """Sorts a signal by the first rule that claims it, checking the rules in registration order.

    An escalation is claimed by its reason, every other kind by its kind. The assessment function
    is asked only when a rule with verdicts claims the signal. A signal no rule claims goes to the
    catch-all, which is the last rule.
    """

    def __init__(self, extensions: Sequence[Extension], assess: AssessFn | None = None) -> None:
        entries = [
            (extension.category, rule) for extension in extensions for rule in extension.rules
        ]
        _check_claims(entries)
        _check_catch_all(entries)
        self._rules = entries[:-1]
        self._catch_all = entries[-1][0]
        self._assess = assess

    async def classify(self, signal: Signal) -> Classification:
        assessment: Assessment | None = None
        for category, rule in self._rules:
            if not _claims(rule, signal):
                continue
            if not rule.verdicts:
                return Classification(category, Source.RULE)
            if self._assess is None:
                continue
            if assessment is None:
                assessment = await self._assess(signal)
            if assessment.verdict in rule.verdicts:
                return Classification(
                    category, Source.REASONER, assessment.note, assessment.verdict
                )
        if assessment is None:
            return Classification(self._catch_all, Source.RULE)
        return Classification(self._catch_all, Source.RULE, assessment.note, assessment.verdict)


def _claims(rule: Rule, signal: Signal) -> bool:
    if signal.kind is Kind.ESCALATION:
        return signal.reason in rule.reasons
    return signal.kind in rule.kinds


def _check_claims(entries: list[_Entry]) -> None:
    reasons: dict[str, Category] = {}
    kinds: dict[Kind, Category] = {}
    for category, rule in entries:
        _claim(reasons, rule.reasons, category, "reason")
        _claim(kinds, rule.kinds, category, "kind")


def _claim[Name](
    claimed: dict[Name, Category], names: frozenset[Name], category: Category, label: str
) -> None:
    for name in names:
        if name in claimed:
            message = f"{label} '{name}' is claimed by both '{claimed[name]}' and '{category}'"
            raise RegistrationError(message)
        claimed[name] = category


def _check_catch_all(entries: list[_Entry]) -> None:
    last = entries[-1][1] if entries else None
    catch_alls = sum(rule.catch_all for _, rule in entries)
    if last is None or catch_alls != 1 or not _is_bare_catch_all(last):
        message = "exactly one catch-all rule, naming nothing, must be the last one registered"
        raise RegistrationError(message)


def _is_bare_catch_all(rule: Rule) -> bool:
    return rule.catch_all and not (rule.reasons or rule.kinds or rule.verdicts)
