"""The rule classifier: the rules of the registered extensions, in order (design §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.classification import Classification, Source

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.classification import Category, Verdict, VerdictFn
    from shikiri_yaku.domain.extension import Extension, Rule
    from shikiri_yaku.domain.signal import Signal

type _Entry = tuple[Category, Rule]


class RegistrationError(Exception):
    """Extensions that cannot be used together; raised when the classifier is built."""


class RuleClassifier:
    """Sorts a signal by the first rule that claims it, checking the rules in registration order.

    The verdict function is asked at most once per signal, and only when a rule with verdicts
    claims the signal. A signal no rule claims goes to the catch-all, which is the last rule.
    """

    def __init__(self, extensions: Sequence[Extension], verdict: VerdictFn | None = None) -> None:
        entries = [
            (extension.category, rule) for extension in extensions for rule in extension.rules
        ]
        _check_claims(entries)
        _check_catch_all(entries)
        self._rules = entries[:-1]
        self._catch_all = entries[-1][0]
        self._verdict = verdict

    async def classify(self, signal: Signal) -> Classification:
        verdict: Verdict | None = None
        for category, rule in self._rules:
            if not _claims(rule, signal):
                continue
            if not rule.verdicts:
                return Classification(category, Source.RULE, None)
            if self._verdict is None:
                continue
            if verdict is None:
                verdict = await self._verdict(signal)
            if verdict in rule.verdicts:
                return Classification(category, Source.REASONER, verdict)
        return Classification(self._catch_all, Source.RULE, verdict)


def _claims(rule: Rule, signal: Signal) -> bool:
    return rule.reasons is None or signal.reason_key in rule.reasons


def _check_claims(entries: list[_Entry]) -> None:
    claimed: dict[str, Category] = {}
    for category, rule in entries:
        for reason in rule.reasons or ():
            if reason in claimed:
                message = (
                    f"reason '{reason}' is claimed by both '{claimed[reason]}' and '{category}'"
                )
                raise RegistrationError(message)
            claimed[reason] = category


def _check_catch_all(entries: list[_Entry]) -> None:
    last = entries[-1][1] if entries else None
    catch_alls = sum(rule.reasons is None for _, rule in entries)
    if last is None or last.reasons is not None or last.verdicts or catch_alls != 1:
        message = "exactly one catch-all rule, without verdicts, must be the last one registered"
        raise RegistrationError(message)
