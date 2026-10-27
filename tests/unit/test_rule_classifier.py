from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.domain.classification import Assessment, Category, Classification, Source, Verdict
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
    }
)
INFRASTRUCTURE_KINDS = frozenset({Kind.ENGINE_DOWN, Kind.ENGINE_FAULT})


class _Stub:
    async def handle(self, signal: Signal) -> Outcome:
        return Forward(note=signal.text)


def _extensions() -> list[Extension]:
    """The rule table of design §5, written as the extensions that would own it."""
    return [
        Extension(
            INFRASTRUCTURE,
            (
                Rule(reasons=INFRASTRUCTURE_REASONS, kinds=INFRASTRUCTURE_KINDS),
                Rule(
                    reasons=frozenset({DECISION_REQUIRED}),
                    verdicts=frozenset({Verdict.HOST_TOOL}),
                ),
            ),
            _Stub(),
        ),
        Extension(REVIEW, (Rule(kinds=frozenset({Kind.PR_REVIEW})),), _Stub()),
        Extension(NOTICE, (Rule(kinds=frozenset({Kind.TASK_DONE, Kind.HEALTH})),), _Stub()),
        Extension(PRODUCT, (Rule(catch_all=True),), _Stub()),
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
def test_every_shikiru_signal_maps_to_its_category(
    kind: Kind, reason: str, expected: Category
) -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(kind, reason))
    assert result == Classification(expected, Source.RULE)


@pytest.mark.parametrize("name", [kind.value for kind in Kind])
def test_unknown_escalation_reason_named_like_a_kind_goes_to_product(name: str) -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(Kind.ESCALATION, name))
    assert result == Classification(PRODUCT, Source.RULE)


@pytest.mark.parametrize(
    ("kind", "reason", "expected"),
    [
        (Kind.TASK_DONE, "agent_error", NOTICE),
        (Kind.ENGINE_FAULT, "pr_closed", INFRASTRUCTURE),
        (Kind.PR_REVIEW, "health", REVIEW),
        (Kind.HEALTH, "pr_review", NOTICE),
    ],
)
def test_kind_signals_ignore_the_reason(kind: Kind, reason: str, expected: Category) -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(kind, reason))
    assert result == Classification(expected, Source.RULE)


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
    async def assess(_: Signal) -> Assessment:
        return Assessment(verdict, note="add the tool")

    result = _classify(
        RuleClassifier(_extensions(), assess), _signal(Kind.ESCALATION, DECISION_REQUIRED)
    )
    assert result == Classification(category, source, "add the tool", verdict)


def test_decision_required_without_an_assessment_function_is_product() -> None:
    result = _classify(RuleClassifier(_extensions()), _signal(Kind.ESCALATION, DECISION_REQUIRED))
    assert result == Classification(PRODUCT, Source.RULE)


def test_two_extensions_claiming_one_reason_fail_at_registration() -> None:
    extensions = _extensions()
    rival = Extension(NOTICE, (Rule(reasons=frozenset({"agent_error"})),), _Stub())
    with pytest.raises(RegistrationError, match="agent_error"):
        RuleClassifier([extensions[0], rival, *extensions[1:]])


def test_two_extensions_claiming_one_kind_fail_at_registration() -> None:
    extensions = _extensions()
    rival = Extension(NOTICE, (Rule(kinds=frozenset({Kind.ENGINE_DOWN})),), _Stub())
    with pytest.raises(RegistrationError, match="engine_down"):
        RuleClassifier([extensions[0], rival, *extensions[1:]])


def test_a_reason_and_a_kind_with_one_name_are_claimed_apart() -> None:
    extensions = _extensions()
    rival = Extension(REVIEW, (Rule(reasons=frozenset({"health"})),), _Stub())
    classifier = RuleClassifier([extensions[0], rival, *extensions[1:]])
    assert _classify(classifier, _signal(Kind.ESCALATION, "health")) == Classification(
        REVIEW, Source.RULE
    )
    assert _classify(classifier, _signal(Kind.HEALTH)) == Classification(NOTICE, Source.RULE)


def test_missing_catch_all_fails_at_registration() -> None:
    with pytest.raises(RegistrationError, match="catch-all"):
        RuleClassifier(_extensions()[:-1])


def test_catch_all_registered_before_other_rules_fails() -> None:
    extensions = _extensions()
    with pytest.raises(RegistrationError, match="catch-all"):
        RuleClassifier([extensions[-1], *extensions[:-1]])


def test_catch_all_that_names_a_reason_fails_at_registration() -> None:
    extensions = _extensions()
    named = Extension(PRODUCT, (Rule(reasons=frozenset({"agent_auth"}), catch_all=True),), _Stub())
    with pytest.raises(RegistrationError, match="catch-all"):
        RuleClassifier([*extensions[:-1], named])
