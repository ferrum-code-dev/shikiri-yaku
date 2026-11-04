"""The system clock, the one adapter that reads time."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


class SystemClock:
    """Reads the wall clock in UTC and sleeps on the running event loop."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
