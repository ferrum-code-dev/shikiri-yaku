"""Open an Expert conversation and run its first turn once."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertRequest
from shikiri_yaku.domain.outbound import Purpose, fit
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    Button,
    ChannelError,
    ChannelRefusedError,
    ExpertError,
    Message,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Expert, Outbox, ShikiruClient

_FAILED: Final = "failed"
_INTERRUPTED: Final = "interrupted"
_OPEN: Final = "open"
_PENDING: Final = "pending"
_DONE: Final = "done"


class ConversationLog(Protocol):
    """The durable state needed to open a conversation."""

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
    def fail(self, update_id: int) -> None: ...
    def opened(self, update_id: int, thread: int) -> None: ...
    def start(self, update_id: int) -> bool: ...
    def finish(self, update_id: int, answer: str, session: str, call: str | None) -> None: ...
    def recover(self) -> list[ConversationRecord]: ...


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
        expert: Expert | None,
        topics: ExpertTopics | None,
        clients: Mapping[str, ShikiruClient],
        scope: Mapping[str, Path],
        directory: Path,
        channel: str,
        group_id: int | None,
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._expert = expert
        self._topics = topics
        self._clients = clients
        self._scope = scope
        self._directory = directory
        self._channel = channel
        self._group_id = group_id

    async def addressed(self, word: str) -> bool:
        return self._topics is not None and await self._topics.addressed(word)

    def recover(self) -> None:
        for row in self._log.recover():
            if row.state == _FAILED:
                destination = Destination(self._channel, str(row.chat), row.origin_thread)
                self._notice(
                    row.update_id,
                    destination,
                    "Topic creation was uncertain. Please ask again.",
                    "creation",
                    row.id,
                )
            elif row.turn_state == _INTERRUPTED:
                destination = Destination(self._channel, str(self._group_id), row.thread_id)
                self._notice(
                    row.update_id,
                    destination,
                    "The Expert turn was interrupted. Please ask again.",
                    "interrupted",
                    row.id,
                )

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
        if self._expert is None or self._topics is None:
            return
        row = await self._open(item, component, text)
        if row is None or row.state != _OPEN:
            return
        thread = row.thread_id
        if thread is None:
            return
        destination = Destination(self._channel, str(self._group_id), thread)
        button = Button("Open topic", url=self._topics.link(thread))
        self._outbox.queue_command_reply(
            [item_destination(self._channel, item)],
            [Message("Open the Expert conversation.", (button,))],
            key=f"ask:open:{item.update_id}",
            now=self._clock.now(),
        )
        if row.turn_state == _PENDING:
            row = await self._turn(item.update_id, row)
            if row is None:
                return
        if row.turn_state == _DONE:
            if row.answer is None:
                return
            answer = ExpertAnswer.model_validate_json(row.answer)
            self._notice(
                item.update_id, destination, fit(answer.reply, MESSAGE_TEXT_LIMIT), "answer", row.id
            )
            if answer.title:
                with suppress(ChannelRefusedError):
                    await self._topics.rename(thread, f"💬 {answer.title}")

    async def _open(
        self, item: Inbound, component: str | None, text: str
    ) -> ConversationRecord | None:
        if self._topics is None:
            return None
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
                thread = await self._topics.create_expert(f"💬 {text}")
            except ChannelError:
                self._log.fail(item.update_id)
                self._notice(
                    item.update_id,
                    item_destination(self._channel, item),
                    "Topic creation was uncertain. Please ask again.",
                    "creation",
                )
                return None
            self._log.opened(item.update_id, thread)
        return self._log.get(item.update_id)

    def _refusal(self, text: str) -> str:
        if self._expert is None:
            return "The Expert is not configured."
        if self._topics is None:
            return "The Expert requires a forum group."
        return "Provide a question after /ask." if not text else ""

    async def _turn(self, update_id: int, row: ConversationRecord) -> ConversationRecord | None:
        if self._expert is None:
            return None
        context: dict[str, object] = {}
        for name in row.scope:
            client = self._clients[name]
            tasks = await client.list_tasks(list(TaskStatus.ACTIVE))
            escalations = await client.list_escalations()
            context[name] = {
                "tasks": [task.model_dump() for task in tasks],
                "escalations": [
                    view.model_dump() for view in escalations if view.escalation.status == _OPEN
                ],
            }
        directory = self._directory / "conversations" / str(row.id)
        directory.mkdir(parents=True, exist_ok=True)
        request = ExpertRequest(
            directory=directory,
            scope={name: self._scope[name] for name in row.scope},
            message=row.text,
            context=json.dumps(context),
        )
        if not self._log.start(update_id):
            return None
        try:
            result = await self._expert.turn(request)
        except ExpertError:
            answer = ExpertAnswer(
                reply="No Expert agent could answer.", title="", tasks=[], findings=""
            )
            self._log.finish(update_id, answer.model_dump_json(), "", None)
        else:
            self._log.finish(
                update_id,
                result.answer.model_dump_json(),
                json.dumps(asdict(result.session)),
                None if result.call is None else json.dumps(asdict(result.call)),
            )
        return self._log.get(update_id)

    def _notice(
        self,
        update_id: int,
        destination: Destination,
        text: str,
        kind: str,
        conversation_id: int | None = None,
    ) -> None:
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [destination],
            Message(text),
            key=f"ask:{kind}:{update_id}",
            now=self._clock.now(),
            conversation_id=conversation_id,
        )
