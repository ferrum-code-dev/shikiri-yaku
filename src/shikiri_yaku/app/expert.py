"""Open an Expert conversation: journal the request, create its topic, then run its first turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.app.expert_turns import CREATION_UNCERTAIN, queue_expert_notice
from shikiri_yaku.ports import Button, ChannelError, Message

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.app.expert_turns import ExpertTurns
    from shikiri_yaku.domain.expert import ConversationRecord, TurnRecord
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox

_OPEN: Final = "open"


class ConversationLog(Protocol):
    """The durable state of Expert conversations and turns."""

    def create(
        self,
        update_id: int,
        *,
        chat: int,
        thread: int | None,
        text: str,
        scope: list[str],
        now: datetime,
    ) -> bool: ...
    def get(self, update_id: int) -> ConversationRecord | None: ...
    def conversation(self, conversation_id: int) -> ConversationRecord | None: ...
    def at_thread(self, thread: int) -> ConversationRecord | None: ...
    def turn(self, update_id: int) -> TurnRecord | None: ...
    def oldest_pending(self, conversation_id: int) -> TurnRecord | None: ...
    def history(self, conversation_id: int, *, before: int) -> list[TurnRecord]: ...
    def add_turn(self, conversation_id: int, update_id: int, text: str, now: datetime) -> bool: ...
    def set_effort(self, conversation_id: int, effort: str) -> None: ...
    def fail(self, update_id: int) -> None: ...
    def opened(self, update_id: int, thread: int) -> None: ...
    def start(self, update_id: int) -> bool: ...
    def finish(
        self, update_id: int, answer: str, session: str | None, call: str | None, now: datetime
    ) -> None: ...
    def touch(self, conversation_id: int, now: datetime) -> None: ...
    def recover_creations(self) -> list[ConversationRecord]: ...
    def recover_turns(self) -> list[TurnRecord]: ...


class ExpertTopics(Protocol):
    """The channel operations for an Expert topic."""

    async def create_expert(self, name: str) -> int: ...
    async def rename(self, thread_id: int, name: str) -> None: ...
    def link(self, thread_id: int) -> str: ...
    async def addressed(self, word: str) -> bool: ...


class Ask:
    """Journal a request before creating its topic and starting its first turn."""

    def __init__(
        self,
        log: ConversationLog,
        outbox: Outbox,
        clock: Clock,
        *,
        configured: bool,
        topics: ExpertTopics | None,
        turns: ExpertTurns | None,
        scope: Mapping[str, Path],
        channel: str,
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._configured = configured
        self._topics = topics
        self._turns = turns
        self._scope = scope
        self._channel = channel

    async def addressed(self, word: str) -> bool:
        return self._topics is not None and await self._topics.addressed(word)

    async def handle(self, item: Inbound, component: str | None) -> None:
        words = (item.text or "").split(maxsplit=1)
        text = words[1].strip() if len(words) > 1 else ""
        reason = self._refusal(text)
        if reason:
            queue_command_reply(
                self._outbox,
                [item_destination(self._channel, item)],
                reason,
                key=f"ask:refusal:{item.update_id}",
                now=self._clock.now(),
            )
            return
        if self._topics is None or self._turns is None:
            return
        row = await self._open(self._topics, item, component, text)
        if row is None or row.state != _OPEN or row.thread_id is None:
            return
        button = Button("Open topic", url=self._topics.link(row.thread_id))
        self._outbox.queue_command_reply(
            [item_destination(self._channel, item)],
            [Message("Open the Expert conversation.", (button,))],
            key=f"ask:open:{item.update_id}",
            now=self._clock.now(),
            conversation_id=row.id,
        )
        await self._turns.run(row.id, item.update_id)

    async def _open(
        self, topics: ExpertTopics, item: Inbound, component: str | None, text: str
    ) -> ConversationRecord | None:
        names = [component] if component is not None else list(self._scope)
        created = self._log.create(
            item.update_id,
            chat=item.chat,
            thread=item.thread,
            text=text,
            scope=names,
            now=self._clock.now(),
        )
        if created:
            try:
                thread = await topics.create_expert(f"💬 {text}")
            except ChannelError:
                self._log.fail(item.update_id)
                queue_expert_notice(
                    self._outbox,
                    self._clock,
                    item_destination(self._channel, item),
                    CREATION_UNCERTAIN,
                    key=f"ask:creation:{item.update_id}",
                )
                return None
            self._log.opened(item.update_id, thread)
        return self._log.get(item.update_id)

    def _refusal(self, text: str) -> str:
        if not self._configured:
            return "The Expert is not configured."
        if self._topics is None:
            return "The Expert requires a forum group."
        return "Provide a question after /ask." if not text else ""
