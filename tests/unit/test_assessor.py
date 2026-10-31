"""The Reasoner as the classifier uses it: a failing ladder, the prompt's facts, verdicts (§5, §8).

The classifier, the assessor, the ladder and the health source are the real ones. The rung and the
host's shell are fakes, and the journal is in memory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku.app.assessor import REASONER_FAILED_NOTE, Assessor
from shikiri_yaku.app.health_source import REASONER_SCOPE, HealthSource
from shikiri_yaku.app.host_facts import HostFacts
from shikiri_yaku.app.inbound_service import OPERATOR_SCOPE
from shikiri_yaku.app.reasoner_ladder import LadderReasoner
from shikiri_yaku.domain.classification import Assessment, Source, Verdict
from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.rules import (
    DECISION_REQUIRED,
    HOST_TOOL_RULE,
    INFRASTRUCTURE,
)
from shikiri_yaku.extensions.product.extension import PRODUCT, product_extension
from shikiri_yaku.journal import CursorStore, Database, JournalSources, SignalStore
from shikiri_yaku.ports import CommandResult, CommandRunner, ReasonerError
from tests.support.clock import NapClock

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.domain.classification import Category

COOLDOWN: Final = timedelta(minutes=5)
SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 5
HOST_OUTPUT: Final = "os=Darwin 25.3.0\nmanager=brew\npath=/usr/bin\n"
HOST_TOOL_ANSWER: Final = Assessment(
    verdict=Verdict.HOST_TOOL,
    note="install ffmpeg with brew",
    tool="ffmpeg",
    manager="brew",
    package="ffmpeg",
    why="ffmpeg is not on the PATH",
)
DECISION: Final = Signal(
    project="api",
    kind=Kind.ESCALATION,
    source_id="esc-1",
    reason=DECISION_REQUIRED,
    text="the build needs ffmpeg",
    escalation_id="esc-1",
)


class _Host(CommandRunner):
    """The host's login shell, which answers every command with the same facts."""

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del argv, timeout_s, cwd, env
        return CommandResult(returncode=0, stdout=HOST_OUTPUT, stderr="")


class _Rung:
    """A rung that gives `answer`, or fails each call when it is None, and keeps each prompt."""

    name = "claude:haiku"

    def __init__(self, answer: Assessment | None) -> None:
        self.answer = answer
        self.facts: list[list[str]] = []

    async def assess_blocker(self, signal: Signal, host_facts: Sequence[str]) -> Assessment:
        del signal
        self.facts.append(list(host_facts))
        if self.answer is None:
            message = f"{self.name} gave no usable answer: its call exited with code 1"
            raise ReasonerError(message)
        return self.answer

    async def probe(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class _Wiring:
    clock: NapClock
    rung: _Rung
    signals: SignalStore
    classifier: RuleClassifier


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _wire(journal: Database, answer: Assessment | None, *, reasoner: bool = True) -> _Wiring:
    clock = NapClock()
    signals = SignalStore(journal)
    sources = JournalSources(CursorStore(journal, signals), signals)
    health = HealthSource(OPERATOR_SCOPE, sources, clock, link_unreachable_min=1)
    rung = _Rung(answer)
    ladder = LadderReasoner([rung], clock, cooldown=COOLDOWN, max_parallel=1)
    assessor = Assessor(ladder, HostFacts(_Host(), SHELL, TIMEOUT_S), health)
    infrastructure = Extension(INFRASTRUCTURE, (HOST_TOOL_RULE,), InfrastructureHandler(()))
    assess = assessor.assess if reasoner else None
    classifier = RuleClassifier([infrastructure, product_extension()], assess)
    return _Wiring(clock, rung, signals, classifier)


def _open_reasoner_signals(signals: SignalStore) -> int:
    return sum(
        stored.project == OPERATOR_SCOPE
        and stored.kind == Kind.HEALTH.value
        and stored.reason == REASONER_SCOPE
        for stored in signals.new()
    )


def _classify(wiring: _Wiring) -> tuple[Category, Verdict | None]:
    classification = asyncio.run(wiring.classifier.classify(DECISION))
    return classification.category, classification.verdict


def test_when_every_rung_fails_the_decision_goes_to_product_with_the_reasoner_note(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=None)

    classification = asyncio.run(wiring.classifier.classify(DECISION))

    assert classification.category == PRODUCT
    assert classification.note.startswith(REASONER_FAILED_NOTE)


def test_repeated_failures_keep_one_reasoner_signal_open(memory: Database) -> None:
    wiring = _wire(memory, answer=None)

    for _ in range(3):
        wiring.clock.advance(COOLDOWN + COOLDOWN)
        _classify(wiring)

    assert len(wiring.rung.facts) == 3
    assert _open_reasoner_signals(wiring.signals) == 1


def test_a_later_answer_closes_the_reasoner_signal_and_the_next_outage_opens_one(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=None)
    _classify(wiring)

    wiring.rung.answer = HOST_TOOL_ANSWER
    wiring.clock.advance(COOLDOWN + COOLDOWN)
    _classify(wiring)
    assert _open_reasoner_signals(wiring.signals) == 0

    wiring.rung.answer = None
    wiring.clock.advance(COOLDOWN + COOLDOWN)
    _classify(wiring)
    assert _open_reasoner_signals(wiring.signals) == 1


def test_host_facts_reach_the_prompt_of_the_call(memory: Database) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER)

    _classify(wiring)

    assert any("Darwin 25.3.0" in line for line in wiring.rung.facts[0])


@pytest.mark.parametrize(
    ("verdict", "category"),
    [
        (Verdict.HOST_TOOL, INFRASTRUCTURE),
        (Verdict.PROJECT_DEPENDENCY, PRODUCT),
        (Verdict.OTHER, PRODUCT),
    ],
)
def test_the_reasoners_verdict_picks_the_category(
    memory: Database, verdict: Verdict, category: Category
) -> None:
    wiring = _wire(memory, answer=Assessment(verdict=verdict, note="a suggestion"))

    assert _classify(wiring) == (category, verdict)


def test_without_a_reasoner_a_decision_goes_to_product_and_the_ladder_is_not_asked(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER, reasoner=False)

    classification = asyncio.run(wiring.classifier.classify(DECISION))

    assert classification.category == PRODUCT
    assert classification.source is Source.RULE
    assert classification.verdict is None
    assert wiring.rung.facts == []
