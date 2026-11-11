"""The Expert ladder and its provider handoffs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from pydantic import ValidationError

from shikiri_yaku.domain.expert import ExpertAnswer
from shikiri_yaku.ports import ExpertError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from shikiri_yaku.domain.expert import ExpertRequest, ExpertResult
    from shikiri_yaku.ports import Clock, ExpertRung


class LadderExpert:
    """Keeps the last successful rung and resumes or hands off on failure."""

    def __init__(
        self,
        rungs: Sequence[ExpertRung],
        clock: Clock,
        *,
        cooldown: timedelta,
        max_parallel: int,
        handoff_turns: int,
        timeout_s: float,
    ) -> None:
        self._rungs = tuple(rungs)
        self._clock = clock
        self._cooldown = cooldown
        self._slots = asyncio.Semaphore(max_parallel)
        self._handoff_turns = handoff_turns
        self._timeout_s = timeout_s
        self._failed: dict[int, datetime] = {}

    async def turn(self, request: ExpertRequest) -> ExpertResult:
        async with self._slots:
            return await self._climb(request)

    async def _climb(self, request: ExpertRequest) -> ExpertResult:
        previous = request.session
        start = previous.rung if previous is not None else 0
        reasons: list[str] = []
        for index in range(start, len(self._rungs)):
            rung = self._rungs[index]
            if index in self._failed and self._clock.now() < self._failed[index]:
                reasons.append(f"{rung.name}: cooling down")
                continue
            same = previous is not None and previous.provider == rung.provider
            session_id = previous.session_id if same and previous is not None else None
            handoff = None
            if previous is not None and not same:
                turns = request.turns[-self._handoff_turns :]
                handoff = "\n".join(
                    [f"Findings: {previous.findings}"]
                    + [f"PO: {turn.message}\nExpert: {turn.reply}" for turn in turns]
                )
            try:
                async with asyncio.timeout(self._timeout_s):
                    result = await rung.turn(request, session_id=session_id, handoff=handoff)
                answer = ExpertAnswer.model_validate(result.answer.model_dump())
                _validate_scope(answer, request, rung.name)
                return replace(result, answer=answer, session=replace(result.session, rung=index))
            except (ExpertError, TimeoutError, ValidationError) as error:
                self._failed[index] = self._clock.now() + self._cooldown
                reasons.append(f"{rung.name}: {error}")
        raise ExpertError("; ".join(reasons) or "no Expert rung available")


def _validate_scope(answer: ExpertAnswer, request: ExpertRequest, name: str) -> None:
    if any(task.component not in request.scope for task in answer.tasks):
        message = f"{name}: task outside scope"
        raise ExpertError(message)
