from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku.adapters.reasoner_cli import CodexRung
from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.app.router import Router
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.bootstrap import schedule_log
from shikiri_yaku.domain.classification import (
    CallRecord,
    Category,
    Classification,
    Source,
    Verdict,
)
from shikiri_yaku.domain.extension import Extension, Rule
from shikiri_yaku.domain.outcome import Forward, Informed, Notified, Resolved, WaitUntil
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.journal import (
    Database,
    JournalRouting,
    NewSignal,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from shikiri_yaku.domain.classification import Assessment, Classifier
    from shikiri_yaku.domain.outcome import Outcome

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TIMEOUT_S = 0.05
RETRY_S = 30
INFRA = Category("infrastructure")


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Classifier:
    def __init__(
        self,
        category: Category = INFRA,
        *,
        failing: bool = False,
        note: str = "",
        call: CallRecord | None = None,
    ) -> None:
        self.category = category
        self.failing = failing
        self.note = note
        self.call = call

    async def classify(self, signal: Signal) -> Classification:
        del signal
        if self.failing:
            message = "model is down"
            raise RuntimeError(message)
        return Classification(
            self.category, Source.REASONER, self.note, Verdict.HOST_TOOL, self.call
        )


class _Handler:
    def __init__(self, *outcomes: Outcome | Exception) -> None:
        self.outcomes = list(outcomes)
        self.handled: list[Signal] = []

    async def handle(self, signal: Signal) -> Outcome:
        self.handled.append(signal)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Hanging:
    async def handle(self, signal: Signal) -> Outcome:
        del signal
        await asyncio.Event().wait()
        return Resolved("late")


class _FirstHangs:
    def __init__(self) -> None:
        self.calls = 0
        self.gate = asyncio.Event()

    async def handle(self, signal: Signal) -> Outcome:
        del signal
        self.calls += 1
        if self.calls == 1:
            await self.gate.wait()
        return Resolved("done")


class _Fallback:
    def __init__(self, *, informs: bool = False) -> None:
        self.forwarded: list[tuple[str, str]] = []
        self.suggestions: list[str] = []
        self._informs = informs

    async def forward(
        self, number: int, signal: Signal, note: str, suggestion: str, *, standing: bool
    ) -> bool:
        del number, standing
        self.forwarded.append((signal.id, note))
        self.suggestions.append(suggestion)
        return self._informs


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.signals = SignalStore(database)
        self.fallback = _Fallback()

    def router(self, classifier: Classifier, **handlers: object) -> Router:
        return Router(
            classifier,
            {Category(name): handler for name, handler in handlers.items()},  # type: ignore[misc]
            self.fallback,
            JournalRouting(self.database, self.signals),
            _Clock(),
            handler_timeout_s=TIMEOUT_S,
            forward_retry_s=RETRY_S,
        )

    def new(self, name: str) -> tuple[int, Signal]:
        signal = Signal("api", Kind.ESCALATION, name, "agent_error", "x", "T-1", name)
        number = self.signals.insert_if_absent(
            NewSignal(signal.id, "api", "escalation", "agent_error", "x", "T-1", name), NOW
        ).row_id
        return number, signal

    def stored(self, number: int):
        stored = self.signals.get(number)
        assert stored is not None
        return stored


@pytest.fixture
def world(memory: Database) -> _World:
    return _World(memory)


async def _route(router: Router, number: int, signal: Signal) -> None:
    router.submit(number, signal)
    await router.join()


@pytest.mark.parametrize(
    ("classifier", "handler", "note"),
    [
        (_Classifier(), _Handler(ValueError("boom")), "ValueError: boom"),
        (_Classifier(), _Hanging(), "timed out"),
        (_Classifier(Category("access")), _Handler(), "category 'access'"),
        (_Classifier(failing=True), _Handler(), "RuntimeError: model is down"),
    ],
    ids=["raises", "times out", "unregistered category", "classifier fails"],
)
def test_a_failure_is_forwarded_with_its_cause(
    world: _World, classifier: _Classifier, handler: object, note: str
) -> None:
    number, signal = world.new("E1")
    router = world.router(classifier, infrastructure=handler)

    asyncio.run(_route(router, number, signal))

    assert [(sent_id, note in sent_note) for sent_id, sent_note in world.fallback.forwarded] == [
        (signal.id, True)
    ]
    assert world.stored(number).state is SignalState.FORWARDED
    assert note in (world.stored(number).notes or "")


def test_a_forward_and_a_resolution_end_the_signal(world: _World) -> None:
    done, forwarded = world.new("E1"), world.new("E2")
    handler = _Handler(Resolved("retried"), Forward("could not fix it"))
    router = world.router(_Classifier(), infrastructure=handler)

    async def both() -> None:
        await _route(router, *done)
        await _route(router, *forwarded)

    asyncio.run(both())

    assert world.stored(done[0]).state is SignalState.RESOLVED
    assert world.stored(done[0]).category == INFRA
    assert world.stored(forwarded[0]).state is SignalState.FORWARDED
    assert world.fallback.forwarded == [(forwarded[1].id, "could not fix it")]


def test_a_notified_signal_waits_for_the_po_like_a_forward_but_sends_nothing_itself(
    world: _World,
) -> None:
    number, signal = world.new("E1")
    router = world.router(_Classifier(), infrastructure=_Handler(Notified("sent the review")))

    asyncio.run(_route(router, number, signal))

    assert world.stored(number).state is SignalState.FORWARDED
    assert "sent the review" in (world.stored(number).notes or "")
    assert world.fallback.forwarded == []


def test_an_informed_outcome_closes_its_signal_and_sends_nothing_itself(world: _World) -> None:
    number, signal = world.new("E1")
    router = world.router(_Classifier(), infrastructure=_Handler(Informed("sent the notice")))

    asyncio.run(_route(router, number, signal))

    assert world.stored(number).state is SignalState.CLOSED
    assert "sent the notice" in (world.stored(number).notes or "")
    assert world.fallback.forwarded == []


def test_a_forward_whose_message_only_informs_closes_its_signal(world: _World) -> None:
    world.fallback = _Fallback(informs=True)
    number, signal = world.new("E1")
    router = world.router(_Classifier(), infrastructure=_Handler(Forward("the engine is down")))

    asyncio.run(_route(router, number, signal))

    assert world.stored(number).state is SignalState.CLOSED
    assert world.fallback.forwarded == [(signal.id, "the engine is down")]


def test_a_signal_is_handled_once(world: _World) -> None:
    number, signal = world.new("E1")
    handler = _Handler(Resolved("first"), Resolved("second"))
    router = world.router(_Classifier(), infrastructure=handler)

    async def twice() -> None:
        await _route(router, number, signal)
        await _route(router, number, signal)

    asyncio.run(twice())

    assert len(handler.handled) == 1


def test_a_slow_handler_does_not_hold_another_signal(world: _World) -> None:
    slow, quick = world.new("E1"), world.new("E2")
    handler = _FirstHangs()
    router = world.router(_Classifier(), infrastructure=handler)

    async def run() -> tuple[SignalState, SignalState]:
        held = router.submit(*slow)
        await router.submit(*quick)
        states = (world.stored(slow[0]).state, world.stored(quick[0]).state)
        handler.gate.set()
        await held
        return states

    assert asyncio.run(run()) == (SignalState.HANDLING, SignalState.RESOLVED)


def test_a_parked_signal_keeps_its_due_time_and_wakes_its_handler(world: _World) -> None:
    number, signal = world.new("E1")
    wake_at = NOW + timedelta(hours=2)
    handler = _Handler(WaitUntil(wake_at, "quota resets at 11:00"), Resolved("retried"))
    router = world.router(_Classifier(), infrastructure=handler)

    asyncio.run(_route(router, number, signal))

    stored = world.stored(number)
    assert (stored.state, stored.handler) == (SignalState.PARKED, INFRA)
    assert stored.wake_at == wake_at.isoformat()

    class _Later(_Clock):
        def now(self) -> datetime:
            return wake_at

    restarted = world.router(_Classifier(failing=True), infrastructure=handler)

    async def wake() -> None:
        scheduler = Scheduler(
            schedule_log(world.database, StderrReporter()),
            restarted,
            _Later(),
            interval_s=1,
            reminder_first_h=4,
            reminder_repeat_h=24,
        )
        await scheduler.tick()
        await restarted.join()

    asyncio.run(wake())

    assert world.stored(number).state is SignalState.RESOLVED
    assert handler.handled == [signal, signal]


def test_the_classification_note_reaches_the_forward_also_after_a_park(world: _World) -> None:
    number, signal = world.new("E1")
    wake_at = NOW + timedelta(hours=1)
    note = "model-written: install ripgrep"
    handler = _Handler(WaitUntil(wake_at, "waiting"), Forward("action refused"))
    router = world.router(_Classifier(note=note), infrastructure=handler)

    async def park_then_wake() -> None:
        await _route(router, number, signal)
        parked = schedule_log(world.database, StderrReporter()).due_parked(wake_at)[0]
        assert schedule_log(world.database, StderrReporter()).claim(parked.number, wake_at)
        await router.wake(parked)
        await router.join()

    asyncio.run(park_then_wake())

    assert world.stored(number).classification_note == note
    assert world.stored(number).classified_by == "reasoner"
    assert world.fallback.suggestions == [note]


def test_the_call_record_of_a_reasoner_verdict_is_kept_in_the_verdict_record(world: _World) -> None:
    number, signal = world.new("E3")
    call = CallRecord(duration_ms=900, cost_usd=0.01, usage={"output_tokens": 35})
    router = world.router(_Classifier(call=call), infrastructure=_Handler(Forward("refused")))

    asyncio.run(_route(router, number, signal))

    row = world.database.connection.execute(
        "SELECT verdict_call FROM signals WHERE number = ?", (number,)
    ).fetchone()
    assert json.loads(row["verdict_call"]) == {
        "duration_ms": 900,
        "cost_usd": 0.01,
        "usage": {"output_tokens": 35},
    }


_CODEX_ANSWER: Final = json.dumps(
    {
        "verdict": "host_tool",
        "tool": "ffmpeg",
        "manager": "brew",
        "package": "ffmpeg",
        "why": "ffmpeg is not on the PATH",
        "suggestion": "install ffmpeg with brew",
    }
)


class _CodexRunner(CommandRunner):
    """Plays `codex exec`: writes the answer file, and prints the token count as Codex does."""

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        answer = Path(argv[argv.index("--output-last-message") + 1])
        await asyncio.to_thread(answer.write_text, _CODEX_ANSWER, encoding="utf-8")
        return CommandResult(returncode=0, stdout="", stderr="tokens used\n15,607\n")


def test_the_tokens_codex_prints_reach_the_verdict_record(world: _World, tmp_path: Path) -> None:
    number, signal = world.new("E5")
    rung = CodexRung(
        "gpt-5",
        _CodexRunner(),
        effort=None,
        timeout_s=TIMEOUT_S,
        input_max_chars=4000,
        scratch_root=tmp_path,
    )

    async def assess(escalation: Signal) -> Assessment:
        return await rung.assess_blocker(escalation, [])

    host_tool = Rule(reasons=frozenset({"agent_error"}), verdicts=frozenset({Verdict.HOST_TOOL}))
    classifier = RuleClassifier(
        [
            Extension(INFRA, (host_tool,), _Handler(Forward("refused"))),
            Extension(Category("product"), (Rule(catch_all=True),), _Handler()),
        ],
        assess,
    )
    router = world.router(classifier, infrastructure=_Handler(Forward("refused")))

    asyncio.run(_route(router, number, signal))

    assert world.stored(number).category == INFRA
    row = world.database.connection.execute(
        "SELECT verdict_call FROM signals WHERE number = ?", (number,)
    ).fetchone()
    assert json.loads(row["verdict_call"]) == {
        "duration_ms": None,
        "cost_usd": None,
        "usage": {"tokens_used": 15607},
    }
