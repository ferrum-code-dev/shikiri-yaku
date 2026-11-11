"""Ends an Expert conversation: stop its work, delete its topic, then erase its rows and files."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    TOPIC_GONE_REASON,
    ChannelError,
    ChannelRefusedError,
    Message,
)

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from pathlib import Path

    from shikiri_yaku.app.expert_runs import ConversationRuns
    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.ports import Clock, Outbox

_REFUSED_DELETION: Final = (
    "The channel refused to delete the Expert topic of conversation {number}. The conversation "
    "stays, and the deletion is tried again."
)
_DELETION_KEY: Final = "expert:deletion:{number}"


class EndingLog(Protocol):
    """The durable state an ending reads and removes."""

    def begin_ending(self, conversation_id: int) -> bool: ...
    def conversation(self, conversation_id: int) -> ConversationRecord | None: ...
    def idle_before(self, cutoff: datetime) -> list[ConversationRecord]: ...
    def ending(self) -> list[ConversationRecord]: ...
    def erase(self, conversation_id: int, *, topic_chat: int) -> None: ...


class TopicDeletion(Protocol):
    """The one call of the channel an ending makes; a refusal is ChannelRefusedError."""

    async def delete(self, thread_id: int) -> None: ...


class ExpertEnding:
    """Every way a conversation ends: its topic closed, /close, expiry, or a topic that is gone.

    The conversation is marked ending first, so nothing new reaches it. Its running agent call is
    stopped and its queued work drained before its topic is deleted. A refused deletion keeps the
    rows and is tried again by `sweep`; the PO is told once. Only the rows and directory of this
    conversation are touched.
    """

    def __init__(
        self,
        log: EndingLog,
        runs: ConversationRuns,
        topics: TopicDeletion,
        outbox: Outbox,
        clock: Clock,
        *,
        directory: Path,
        channel: str,
        group_id: int,
        retention: timedelta,
    ) -> None:
        self._log = log
        self._runs = runs
        self._topics = topics
        self._outbox = outbox
        self._clock = clock
        self._directory = directory
        self._channel = channel
        self._group_id = group_id
        self._retention = retention

    async def end(self, conversation_id: int) -> None:
        """End the conversation: its topic is deleted now, or retried by `sweep` if refused."""
        self._log.begin_ending(conversation_id)
        await self._runs.stop(conversation_id)
        row = self._log.conversation(conversation_id)
        if row is None or row.thread_id is None:
            return
        try:
            await self._topics.delete(row.thread_id)
        except ChannelRefusedError as refusal:
            if str(refusal) != TOPIC_GONE_REASON:
                self._report_refusal(row)
                return
        except ChannelError:
            return
        self._erase(row)

    async def topic_gone(self, conversation_id: int, destination: Destination) -> None:
        """Erase a conversation whose topic no longer exists; there is nothing to delete.

        The directory goes before the rows: the closing rows are the retry state, so a crash or a
        failed removal leaves them for `sweep` to finish. Only a failed send to this topic
        qualifies.
        """
        row = self._log.conversation(conversation_id)
        if (
            row is None
            or destination.chat != str(self._group_id)
            or destination.thread != row.thread_id
        ):
            return
        self._log.begin_ending(conversation_id)
        await self._runs.stop(conversation_id)
        self._erase(row)

    async def sweep(self) -> None:
        """Retry the deletions not yet done, then end the conversations idle past retention."""
        for row in self._log.ending():
            await self.end(row.id)
        cutoff = self._clock.now() - self._retention
        for row in self._log.idle_before(cutoff):
            await self.end(row.id)

    def _erase(self, row: ConversationRecord) -> None:
        self._remove_directory(row.id)
        self._log.erase(row.id, topic_chat=self._group_id)

    def _remove_directory(self, conversation_id: int) -> None:
        path = conversation_directory(self._directory, conversation_id)
        if path.exists():
            shutil.rmtree(path)

    def _report_refusal(self, row: ConversationRecord) -> None:
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [Destination(self._channel, str(self._group_id))],
            Message(_REFUSED_DELETION.format(number=row.id)),
            key=_DELETION_KEY.format(number=row.id),
            now=self._clock.now(),
        )
