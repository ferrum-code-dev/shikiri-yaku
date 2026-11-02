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
from shikiri_yaku.app.reasoner_ladder import LadderReasoner
from shikiri_yaku.app.status import Status
from shikiri_yaku.domain.classification import Assessment, InstallProposal, Source, Verdict
from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import OPERATOR_SCOPE, Kind, Signal
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.rules import (
    DECISION_REQUIRED,
    HOST_TOOL_RULE,
    INFRASTRUCTURE,
)
from shikiri_yaku.extensions.product.extension import PRODUCT, product_extension
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalRouting,
    JournalSources,
    NewSignal,
    SignalStore,
)
from shikiri_yaku.ports import CommandResult, CommandRunner, ReasonerError
from tests.support.clock import NapClock

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.domain.classification import Category, Classification

COOLDOWN: Final = timedelta(minutes=5)
SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 5
DEADLINE_S: Final = 30.0
SHORT_DEADLINE_S: Final = 0.05
# keep: the cap only stops a broken deadline from hanging the run; the assertions then fail
HANG_CAP_S: Final = 10
# keep: a call cut by its deadline finishes well inside this; a broken deadline does not
CUT_BEFORE_S: Final = 5
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


async def _never_returns() -> None:
    async with asyncio.timeout(HANG_CAP_S):
        await asyncio.Event().wait()


async def _classify_before_cut(classifier: RuleClassifier) -> Classification:
    async with asyncio.timeout(CUT_BEFORE_S):
        return await classifier.classify(DECISION)


class _Host(CommandRunner):
    """The host's login shell, which answers every command with the same facts, or never."""

    def __init__(self, *, hangs: bool = False) -> None:
        self.hangs = hangs

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
        if self.hangs:
            await _never_returns()
        return CommandResult(returncode=0, stdout=HOST_OUTPUT, stderr="")


class _Rung:
    """A rung that gives `answer`, or fails each call when it is None, and keeps each prompt.

    A rung that hangs never answers; it records whether the deadline cancelled its call.
    """

    name = "claude:haiku"

    def __init__(self, answer: Assessment | None, *, hangs: bool = False) -> None:
        self.answer = answer
        self.hangs = hangs
        self.cancelled = False
        self.facts: list[list[str]] = []

    async def assess_blocker(self, signal: Signal, host_facts: Sequence[str]) -> Assessment:
        del signal
        self.facts.append(list(host_facts))
        if self.hangs:
            await self._hang()
        if self.answer is None:
            message = f"{self.name} gave no usable answer: its call exited with code 1"
            raise ReasonerError(message)
        return self.answer

    async def probe(self) -> None:
        return

    async def _hang(self) -> None:
        try:
            await _never_returns()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@dataclass(frozen=True, slots=True)
class _Wiring:
    clock: NapClock
    rung: _Rung
    ladder: LadderReasoner
    signals: SignalStore
    classifier: RuleClassifier


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _wire(
    journal: Database,
    answer: Assessment | None,
    *,
    reasoner: bool = True,
    rung_hangs: bool = False,
    host_hangs: bool = False,
    deadline_s: float = DEADLINE_S,
) -> _Wiring:
    clock = NapClock()
    signals = SignalStore(journal)
    sources = JournalSources(CursorStore(journal, signals), signals)
    health = HealthSource(OPERATOR_SCOPE, sources, clock, link_unreachable_min=1)
    rung = _Rung(answer, hangs=rung_hangs)
    ladder = LadderReasoner([rung], clock, cooldown=COOLDOWN, max_parallel=1)
    facts = HostFacts(_Host(hangs=host_hangs), SHELL, TIMEOUT_S)
    assessor = Assessor(ladder, facts, health, deadline_s=deadline_s)
    infrastructure = Extension(INFRASTRUCTURE, (HOST_TOOL_RULE,), InfrastructureHandler(()))
    assess = assessor.assess if reasoner else None
    classifier = RuleClassifier([infrastructure, product_extension()], assess)
    return _Wiring(clock, rung, ladder, signals, classifier)


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


def test_host_tool_proposal_round_trips_from_assessment_to_signal_row(memory: Database) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER)
    classification = asyncio.run(wiring.classifier.classify(DECISION))
    number = wiring.signals.insert_if_absent(
        NewSignal(
            DECISION.id,
            DECISION.project,
            DECISION.kind.value,
            DECISION.reason,
            DECISION.text,
            DECISION.task_id,
            DECISION.escalation_id,
        ),
        wiring.clock.now(),
    ).row_id

    JournalRouting(memory, wiring.signals).begin(
        number, classification, "infrastructure", wiring.clock.now()
    )

    assert wiring.signals.install_proposal(DECISION.id) == InstallProposal(
        tool="ffmpeg",
        manager="brew",
        package="ffmpeg",
        why="ffmpeg is not on the PATH",
    )


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


def test_a_ladder_that_never_answers_is_cut_at_the_deadline_and_the_decision_goes_to_product(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER, rung_hangs=True, deadline_s=SHORT_DEADLINE_S)

    classification = asyncio.run(_classify_before_cut(wiring.classifier))

    assert wiring.rung.cancelled
    assert classification.category == PRODUCT
    assert classification.note.startswith(REASONER_FAILED_NOTE)
    assert _open_reasoner_signals(wiring.signals) == 1


def test_calls_waiting_for_the_one_slot_are_cut_at_the_deadline_and_never_reach_the_rung(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER, rung_hangs=True, deadline_s=SHORT_DEADLINE_S)

    async def two_calls() -> None:
        async with asyncio.timeout(CUT_BEFORE_S):
            await asyncio.gather(*(wiring.classifier.classify(DECISION) for _ in range(2)))

    asyncio.run(two_calls())

    assert len(wiring.rung.facts) == 1
    assert _open_reasoner_signals(wiring.signals) == 1


def test_host_facts_that_never_return_are_cut_at_the_deadline_and_the_rung_is_not_asked(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER, host_hangs=True, deadline_s=SHORT_DEADLINE_S)

    classification = asyncio.run(_classify_before_cut(wiring.classifier))

    assert classification.category == PRODUCT
    assert classification.note.startswith(REASONER_FAILED_NOTE)
    assert wiring.rung.facts == []
    assert wiring.ladder.rung_states()[0].last_failure is None


def test_a_deadline_interrupted_rung_cools_down_and_recovers_after_the_cooldown(
    memory: Database,
) -> None:
    wiring = _wire(memory, answer=HOST_TOOL_ANSWER, rung_hangs=True, deadline_s=SHORT_DEADLINE_S)

    asyncio.run(_classify_before_cut(wiring.classifier))

    (state,) = wiring.ladder.rung_states()
    assert state.last_failure is not None
    assert "timed out" in state.last_failure
    assert state.cooling_until is not None
    sources = JournalSources(CursorStore(memory, wiring.signals), wiring.signals)
    report = asyncio.run(Status({}, sources, wiring.ladder).report())
    assert "claude:haiku: cooling until" in report

    _classify(wiring)
    assert len(wiring.rung.facts) == 1

    wiring.clock.advance(COOLDOWN + COOLDOWN)
    wiring.rung.hangs = False
    assert _classify(wiring) == (INFRASTRUCTURE, Verdict.HOST_TOOL)
    assert len(wiring.rung.facts) == 2
    assert wiring.ladder.rung_states()[0].cooling_until is None
    assert _open_reasoner_signals(wiring.signals) == 0
