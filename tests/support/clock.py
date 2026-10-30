"""A Clock for tests that compose the whole app, whose waits yield for a millisecond only."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from shikiri_yaku.adapters.clock import SystemClock

if TYPE_CHECKING:
    from datetime import datetime

STEP_S: Final = 0.001


class NapClock:
    """The wall clock, with every wait cut to a millisecond, as the service-loop tests cut theirs.

    The composed jobs wait their configured intervals, which are whole seconds at least. A test
    that runs them would wait for seconds per cycle, so a wait here only lets the other jobs run.
    """

    def now(self) -> datetime:
        return SystemClock().now()

    async def sleep(self, seconds: float) -> None:
        del seconds
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(STEP_S):
                await asyncio.Event().wait()
