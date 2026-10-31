"""A Clock for tests that compose the whole app, whose waits yield for a millisecond only."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.adapters.clock import SystemClock

if TYPE_CHECKING:
    from datetime import datetime

STEP_S: Final = 0.001


class NapClock:
    """The wall clock, moved forward by `advance`, with every wait cut to a millisecond.

    The composed jobs wait their configured intervals, which are whole seconds at least. A test
    that runs them would wait for seconds per cycle, so a wait here only lets the other jobs run.
    A test that needs a reset time to pass moves the clock with `advance` instead of waiting.
    """

    def __init__(self) -> None:
        self._moved = timedelta()

    def now(self) -> datetime:
        return SystemClock().now() + self._moved

    def advance(self, by: timedelta) -> None:
        self._moved += by

    async def sleep(self, seconds: float) -> None:
        del seconds
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(STEP_S):
                await asyncio.Event().wait()
