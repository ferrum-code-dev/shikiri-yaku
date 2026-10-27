from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.domain.classification import Category, Classification, Source, Verdict
from shikiri_yaku.domain.extension import Extension, Rule
from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.rule_classifier import RegistrationError, RuleClassifier
from shikiri_yaku.domain.signal import Kind, Signal

if TYPE_CHECKING:
    from shikiri_yaku.domain.outcome import Outcome

PRODUCT = Category("product")
INFRASTRUCTURE = Category("infrastructure")
REVIEW = Category("review")
NOTICE = Category("notice")

DECISION_REQUIRED = "decision_required"
INFRASTRUCTURE_REASONS = frozenset(
    {
        "integration_conflict",
        "integration_blocked",
        "agent_error",
        "agent_stuck",
        "agent_protocol",
        "quota_exhausted",
        "ladder_exhausted",
        "engine_error",
        "engine_down",
        "engine_fault",
    }
)


class _Stub:
    async def handle(self, signal: Signal) -> Outcome:
        return Forward(note=signal.text)


def _extensions() -> list[Extension]:
    """The rule table of design §5, written as the extensions that would own it."""
    return [
        Extension(
            INFRASTRUCTURE,
            (
                Rule(INFRASTRUCTURE_REASONS),
                Rule(frozenset({DECISION_REQUIRED}), frozenset({Verdict.HOST_TOOL})),
            ),
            _Stub(),
        ),
        Extension(REVIEW, (Rule(frozenset({"pr_review"})),), _Stub()),
        Extension(NOTICE, (Rule(frozenset({"task_done", "health"})),), _Stub()),
        Extension(PRODUCT, (Rule(reasons=None),), _Stub()),
    ]


def _signal(kind: Kind, reason: str = "") -> Signal:
    return Signal(project="api", kind=kind, source_id="1", reason=reason, text="question")


def _classify(classifier: RuleClassifier, signal: Signal) -> Classification:
    return asyncio.run(classifier.classify(signal))


SHIKIRU_SIGNALS = [
    (Kind.ESCALATION, "integration_conflict", INFRASTRUCTURE),
    (Kind.ESCALATION, "integration_blocked", INFRASTRUCTURE),
    (Kind.ESCALATION, "agent_error", INFRASTRUCTURE),
    (Kind.ESCALATION, "agent_stuck", INFRASTRUCTURE),
    (Kind.ESCALATION, "agent_protocol", INFRASTRUCTURE),
    (Kind.ESCALATION, "quota_exhausted", INFRASTRUCTURE),
    (Kind.ESCALATION, "ladder_exhausted", INFRASTRUCTURE),
    (Kind.ESCALATION, "engine_error", INFRASTRUCTURE),
    (Kind.ENGINE_DOWN, "", INFRASTRUCTURE),
    (Kind.ENGINE_FAULT, "repeated faults", INFRASTRUCTURE),
    (Kind.PR_REVIEW, "", REVIEW),
    (Kind.TASK_DONE, "", NOTICE),
    (Kind.HEALTH, "", NOTICE),
    (Kind.ESCALATION, "iterations_exhausted", PRODUCT),
    (Kind.ESCALATION, "pr_closed", PRODUCT),
    (Kind.ESCALATION, "dependency_cancelled", PRODUCT),
    (Kind.ESCALATION, "agent_auth", PRODUCT),
    (Kind.ESCALATION, "uncommitted_work", PRODUCT),
    (Kind.ESCALATION, "off_task_branch", PRODUCT),
    (Kind.ESCALATION, "reason_shikiru_adds_later", PRODUCT),
]


@pytest.mark.parametrize(("kind", "reason", "expected"), SHIKIRU_SIGNALS)
def test_every_shikiru_reason_maps_to_its_category(
    kind: Kind, reason: str, expected: Category
) -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(kind, reason))
    assert result == Classification(expected, Source.RULE, None)


@pytest.mark.parametrize(
    ("verdict", "category", "source"),
    [
        (Verdict.HOST_TOOL, INFRASTRUCTURE, Source.REASONER),
        (Verdict.PROJECT_DEPENDENCY, PRODUCT, Source.RULE),
        (Verdict.OTHER, PRODUCT, Source.RULE),
    ],
)
def test_decision_required_follows_the_verdict(
    verdict: Verdict, category: Category, source: Source
) -> None:
    async def ask(_: Signal) -> Verdict:
        return verdict

    result = _classify(
        RuleClassifier(_extensions(), ask), _signal(Kind.ESCALATION, DECISION_REQUIRED)
    )
    assert result == Classification(category, source, verdict)


def test_decision_required_without_a_verdict_function_is_product() -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(Kind.ESCALATION, DECISION_REQUIRED))
    assert result == Classification(PRODUCT, Source.RULE, None)


def test_two_extensions_claiming_one_reason_fail_at_registration() -> None:
    extensions = _extensions()
    rival = Extension(NOTICE, (Rule(frozenset({"agent_error"})),), _Stub())
    with pytest.raises(RegistrationError, match="agent_error"):
        RuleClassifier([extensions[0], rival, *extensions[1:]])


def test_missing_catch_all_fails_at_registration() -> None:
    with pytest.raises(RegistrationError):
        RuleClassifier(_extensions()[:-1])


def test_catch_all_registered_before_other_rules_fails() -> None:
    extensions = _extensions()
    with pytest.raises(RegistrationError):
        RuleClassifier([extensions[-1], *extensions[:-1]])
