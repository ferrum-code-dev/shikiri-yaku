from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from shikiri_yaku.adapters.host_lock import FileHostLock
from tests.support.clock import NapClock

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

POLL_S: Final = 1
WAITS: Final = 3
TIMEOUT_S: Final = 10


class _CountingClock:
    """The test clock, which counts the waits of a lock that is held by someone else."""

    def __init__(self) -> None:
        self.polls = 0
        self._clock = NapClock()

    def now(self) -> datetime:
        return self._clock.now()

    async def sleep(self, seconds: float) -> None:
        self.polls += 1
        await self._clock.sleep(seconds)


async def _until(holds: Callable[[], bool]) -> None:
    while not holds():
        await NapClock().sleep(0)


async def _enter(lock: FileHostLock, events: list[str], name: str) -> None:
    async with lock.hold():
        events.append(f"{name} in")


def test_a_second_holder_waits_until_the_first_releases(tmp_path: Path) -> None:
    path = tmp_path / "host.lock"
    events: list[str] = []

    async def scenario() -> None:
        clock = _CountingClock()
        async with FileHostLock(path, clock, POLL_S).hold():
            events.append("first in")
            waiting = asyncio.ensure_future(
                _enter(FileHostLock(path, clock, POLL_S), events, "second")
            )
            await _until(lambda: clock.polls >= WAITS)
            assert events == ["first in"]
        await waiting

    asyncio.run(scenario())

    assert events == ["first in", "second in"]


def test_a_cancelled_waiter_does_not_keep_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "host.lock"

    async def scenario() -> None:
        clock = _CountingClock()
        async with FileHostLock(path, clock, POLL_S).hold():
            waiter = asyncio.ensure_future(_enter(FileHostLock(path, clock, POLL_S), [], "waiter"))
            await _until(lambda: clock.polls >= WAITS)
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        async with asyncio.timeout(TIMEOUT_S):
            async with FileHostLock(path, clock, POLL_S).hold():
                pass

    asyncio.run(scenario())
