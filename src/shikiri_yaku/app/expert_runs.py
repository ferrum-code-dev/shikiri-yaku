"""The turn lock and running turn of each Expert conversation, shared by turns and endings."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

CONVERSATIONS_DIR: Final = "conversations"


def conversation_directory(root: Path, conversation_id: int) -> Path:
    """Return the directory of one conversation's files under the instance directory."""
    return root / CONVERSATIONS_DIR / str(conversation_id)


class ConversationRuns:
    """Serialize the work on one conversation and let an ending stop its running turn.

    The lock is taken before any await, and asyncio wakes its waiters in order, so turns keep the
    order of arrival. A running turn is tracked as its own task, so stopping it never cancels the
    task of the message that started it.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._turns: dict[int, asyncio.Task[bool]] = {}

    def lock(self, conversation_id: int) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    def track(self, conversation_id: int, turn: asyncio.Task[bool]) -> None:
        self._turns[conversation_id] = turn

    def untrack(self, conversation_id: int) -> None:
        self._turns.pop(conversation_id, None)

    async def stop(self, conversation_id: int) -> None:
        """Cancel the running turn, then wait until the work already queued on the lock is done.

        Callers mark the conversation as ending first, so the work that waits behind this one
        starts nothing and writes nothing.
        """
        if (turn := self._turns.get(conversation_id)) is not None:
            turn.cancel()
        async with self.lock(conversation_id):
            pass
