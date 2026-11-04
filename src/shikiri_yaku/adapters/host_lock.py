"""The host lock permits one host command at a time, across every instance on this host.

The lock is an exclusive flock on a file, so the kernel releases it when the holder exits, even
after a crash. A waiter polls with the Clock, so it stays cancellable while it waits.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from shikiri_yaku.ports import Clock

# keep: the lock file is the user's own, not readable by others
LOCK_FILE_MODE: Final = 0o600


class FileHostLock:
    """Holds the host lock file exclusively, waiting `poll_s` between attempts."""

    def __init__(self, path: Path, clock: Clock, poll_s: float) -> None:
        self._path = path
        self._clock = clock
        self._poll_s = poll_s

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
        try:
            await self._acquire(descriptor)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    async def _acquire(self, descriptor: int) -> None:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                await self._clock.sleep(self._poll_s)
            else:
                return
