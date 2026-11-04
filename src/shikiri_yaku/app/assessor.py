"""The Reasoner as the classifier sees it: host facts in, an assessment out, and it never raises.

A ladder that gives no answer, or no verdict within the deadline, becomes an `other` assessment
whose note says the Reasoner failed, so the signal goes to the product category with that note.
The health source hears about each call, so one signal opens while the ladder fails and closes on
the next answer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from shikiri_yaku.domain.classification import Assessment, Verdict
from shikiri_yaku.ports import ReasonerError

if TYPE_CHECKING:
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.app.host_facts import HostFacts
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Reasoner

REASONER_FAILED_NOTE = "Reasoner failed: "


class Assessor:
    def __init__(
        self, reasoner: Reasoner, facts: HostFacts, health: HealthSource, *, deadline_s: float
    ) -> None:
        self._reasoner = reasoner
        self._facts = facts
        self._health = health
        self._deadline_s = deadline_s

    async def assess(self, signal: Signal) -> Assessment:
        deadline = asyncio.timeout(self._deadline_s)
        try:
            async with deadline:
                host_facts = await self._facts.collect()
                assessment = await self._reasoner.assess_blocker(
                    signal, host_facts.prompt, deadline=deadline
                )
        except ReasonerError as error:
            return self._failed(str(error))
        except TimeoutError:
            return self._failed(f"no verdict within {self._deadline_s:g} seconds")
        self._health.reasoner_recovered()
        return assessment

    def _failed(self, reason: str) -> Assessment:
        self._health.reasoner_failed()
        return Assessment(verdict=Verdict.OTHER, note=f"{REASONER_FAILED_NOTE}{reason}")
