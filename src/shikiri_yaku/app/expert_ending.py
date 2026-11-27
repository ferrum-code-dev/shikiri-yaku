"""Ends an Expert conversation: stop its work, delete its topic, then erase its rows and files."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.app.expert import closed_reply, open_reply_keys
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.domain.expert import MODE_ADMIN, MODE_EXPERT
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.signal import OPERATOR_SCOPE, Kind
from shikiri_yaku.ports import (
    ChannelError,
    ChannelRefusedError,
    Message,
    PlaceGoneError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta
    from pathlib import Path

    from shikiri_yaku.app.expert_runs import ConversationRuns
    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import Clock, Destinations, FinalEdits, Outbox, Rooms

_REFUSED_DELETION: Final = (
    "The channel refused to delete the Expert topic of conversation {number}. The conversation "
    "stays, and the deletion is tried again."
)
_DELETION_KEY: Final = "expert:deletion:{number}"
_CONVERSATION_CLOSING: Final = "closing"


class EndingLog(Protocol):
    """The durable state an ending reads and removes."""

    def begin_ending(
        self,
        conversation_id: int,
        now: datetime,
        *,
        sender: int | None = None,
        update_id: int | None = None,
    ) -> ConversationRecord | None: ...
    def conversation(self, conversation_id: int) -> ConversationRecord | None: ...
    def idle_before(self, cutoff: datetime, mode: str) -> list[ConversationRecord]: ...
    def ending(self) -> list[ConversationRecord]: ...
    def erase(self, conversation_id: int, *, room_place: Destination) -> None: ...


class ExpertEnding:
    """Every way a conversation ends: its topic closed, /close, expiry, or a topic that is gone.

    The conversation is marked ending first, so nothing new reaches it. Its /expert reply is closed
    before that, so the chat shows the closed reply. Its running agent call is stopped and its
    queued work drained before its topic is deleted. A refused deletion keeps the rows and is tried
    again by `sweep`; the PO is told once. The rows and directory of this conversation are erased
    once its closed reply is confirmed or given up, so `sweep` finishes the erasure. Only the rows
    and directory of this conversation are touched.
    """

    def __init__(
        self,
        log: EndingLog,
        runs: ConversationRuns,
        rooms: Rooms,
        outbox: Outbox,
        clock: Clock,
        *,
        places: Destinations,
        directory: Path,
        retention: timedelta,
        admin_idle: timedelta,
        final: FinalEdits,
    ) -> None:
        self._log = log
        self._runs = runs
        self._rooms = rooms
        self._places = places
        self._outbox = outbox
        self._clock = clock
        self._directory = directory
        self._retention = retention
        self._admin_idle = admin_idle
        self._final = final
        self.admin_expiry: Callable[[], None] | None = None

    async def end(
        self, conversation_id: int, *, sender: int | None = None, update_id: int | None = None
    ) -> None:
        """End the conversation: its topic is deleted now, or retried by `sweep` if refused."""
        row = self._log.begin_ending(
            conversation_id, self._clock.now(), sender=sender, update_id=update_id
        )
        if row is None or row.state != _CONVERSATION_CLOSING:
            return
        self._close_reply(row)
        await self._runs.stop(conversation_id)
        if row.room is None:
            return
        try:
            await self._rooms.delete(row.room)
        except ChannelRefusedError as refusal:
            if not isinstance(refusal, PlaceGoneError):
                self._report_refusal(row)
                return
        except ChannelError:
            return
        self._erase_when_settled(row.id, row.room)

    async def topic_gone(self, conversation_id: int, destination: Destination) -> None:
        """Erase a conversation whose topic no longer exists; there is nothing to delete.

        The directory goes before the rows: the closing rows are the retry state, so a crash or a
        failed removal leaves them for `sweep` to finish. Only a failed send to this topic
        qualifies.
        """
        row = self._log.conversation(conversation_id)
        if row is None or row.room is None or self._rooms.room_of(destination) != row.room:
            return
        room = row.room
        row = self._log.begin_ending(conversation_id, self._clock.now())
        if row is None or row.state != _CONVERSATION_CLOSING:
            return
        self._close_reply(row)
        await self._runs.stop(conversation_id)
        self._erase_when_settled(row.id, room)

    async def sweep(self) -> None:
        """Retry the deletions not yet done, then end the conversations idle past their period."""
        if self.admin_expiry is not None:
            self.admin_expiry()
        for row in self._log.ending():
            await self.end(row.id)
        now = self._clock.now()
        for mode, idle in ((MODE_EXPERT, self._retention), (MODE_ADMIN, self._admin_idle)):
            for row in self._log.idle_before(now - idle, mode):
                await self.end(row.id)

    def _close_reply(self, row: ConversationRecord) -> None:
        """Show the fixed ending time and associate any shared-group selection reply."""
        if row.ending_at is None:
            return
        closed = closed_reply(row.ending_at)
        for key in open_reply_keys(row.update_id):
            self._final.settle_notice(key, closed, conversation_id=row.id)

    def _erase_when_settled(self, conversation_id: int, room: str) -> None:
        """Erase the conversation once no copy tagged with it waits for its final edit.

        The rows are the copies' record until the edit is confirmed or given up, so they stay
        until then; `sweep` erases the conversation on a later tick.
        """
        if self._final.pending_for(conversation_id):
            return
        self._remove_directory(conversation_id)
        self._log.erase(conversation_id, room_place=self._rooms.place(room))

    def _remove_directory(self, conversation_id: int) -> None:
        path = conversation_directory(self._directory, conversation_id)
        if path.exists():
            shutil.rmtree(path)

    def _report_refusal(self, row: ConversationRecord) -> None:
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            self._places.for_signal(OPERATOR_SCOPE, Kind.HEALTH),
            Message(_REFUSED_DELETION.format(number=row.id)),
            key=_DELETION_KEY.format(number=row.id),
            now=self._clock.now(),
        )
