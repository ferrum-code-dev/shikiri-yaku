"""The Reasoner's ladder: rung order, the cooldown of a failed rung, the parallel bound (§8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.app.reasoner_ladder import LadderReasoner
from shikiri_yaku.domain.classification import Assessment, Verdict
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.ports import ReasonerError

if TYPE_CHECKING:
    from collections.abc import Sequence

START: Final = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
COOLDOWN: Final = timedelta(minutes=5)
ANSWER: Final = Assessment(verdict=Verdict.OTHER, note="from the answering rung")
_SIGNAL: Final = Signal(
    project="api",
    kind=Kind.ESCALATION,
    source_id="esc-1",
    reason="blocked",
    text="the build needs ffmpeg",
    escalation_id="esc-1",
)
_FACTS: Final = ["OS: macOS 15", "package managers: brew"]


class _Clock:
    def __init__(self) -> None:
        self.moment = START

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Rung:
    """A rung that fails when `down`, and that can be held open to see how many calls overlap."""

    def __init__(self, name: str, *, down: bool = False, gate: asyncio.Event | None = None) -> None:
        self.name = name
        self.down = down
        self.gate = gate
        self.calls = 0
        self.inside = 0
        self.peak = 0
        self.two_inside = asyncio.Event()

    async def assess_blocker(self, signal: Signal, host_facts: Sequence[str]) -> Assessment:
        del signal, host_facts
        self.calls += 1
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        if self.peak >= 2:
            self.two_inside.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.down:
                message = f"{self.name} gave no usable answer: its call exited with code 1"
                raise ReasonerError(message)
            return ANSWER
        finally:
            self.inside -= 1


def test_a_failed_rung_is_skipped_for_the_cooldown_and_tried_again_after_it() -> None:
    clock = _Clock()
    first = _Rung("claude:haiku", down=True)
    second = _Rung("codex:gpt-5")
    ladder = LadderReasoner([first, second], clock, cooldown=COOLDOWN, max_parallel=2)

    async def scenario() -> list[tuple[int, int]]:
        counts: list[tuple[int, int]] = []
        for moved in (timedelta(), timedelta(minutes=4), timedelta(minutes=1)):
            clock.moment += moved
            assert await ladder.assess_blocker(_SIGNAL, _FACTS) == ANSWER
            counts.append((first.calls, second.calls))
        return counts

    assert asyncio.run(scenario()) == [(1, 1), (1, 2), (2, 3)]


def test_when_every_rung_fails_the_error_names_each_rung() -> None:
    ladder = LadderReasoner(
        [_Rung("claude:haiku", down=True), _Rung("codex:gpt-5", down=True)],
        _Clock(),
        cooldown=COOLDOWN,
        max_parallel=2,
    )

    with pytest.raises(ReasonerError) as raised:
        asyncio.run(ladder.assess_blocker(_SIGNAL, _FACTS))

    assert "claude:haiku gave no usable answer" in str(raised.value)
    assert "codex:gpt-5 gave no usable answer" in str(raised.value)


def test_no_more_than_max_parallel_calls_run_at_once() -> None:
    async def scenario() -> tuple[int, list[Assessment]]:
        gate = asyncio.Event()
        rung = _Rung("claude:haiku", gate=gate)
        ladder = LadderReasoner([rung], _Clock(), cooldown=COOLDOWN, max_parallel=2)
        calls = [asyncio.create_task(ladder.assess_blocker(_SIGNAL, _FACTS)) for _ in range(5)]
        await rung.two_inside.wait()
        gate.set()
        return rung.peak, await asyncio.gather(*calls)

    peak, answers = asyncio.run(scenario())

    assert peak == 2
    assert answers == [ANSWER] * 5
