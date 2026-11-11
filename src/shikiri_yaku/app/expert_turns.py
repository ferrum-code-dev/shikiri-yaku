"""Turns of an Expert conversation and the commands of its topic."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertRequest, ExpertTurn
from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    ChannelRefusedError,
    ExpertError,
    Message,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from shikiri_yaku.app.expert import ConversationLog, ExpertTopics
    from shikiri_yaku.app.expert_ending import ExpertEnding
    from shikiri_yaku.app.expert_proposals import ExpertProposals
    from shikiri_yaku.app.expert_runs import ConversationRuns
    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Expert, Outbox, ShikiruClient

CREATION_UNCERTAIN: Final = "Topic creation was uncertain. Please ask again."
SUMMARY_PROMPT: Final = "Summarize this conversation in two or three short sentences."
_INTERRUPTED: Final = "The Expert turn was interrupted. Please ask again."
_NO_ANSWER: Final = "No Expert agent could answer."
_EFFORT: Final = "/effort"
_EFFORT_USAGE: Final = "Usage: /effort [level]"
_SUMMARIZE: Final = "/summarize"
_CLOSE: Final = "/close"
_HELP: Final = (
    "/effort [level] · show this conversation's effort, or set it from the next turn\n"
    f"{_SUMMARIZE} · a very short summary of the conversation\n"
    f"{_CLOSE} · close this conversation\n"
    "/help · this list"
)
_PENDING: Final = "pending"
_DONE: Final = "done"
_ESCALATION_OPEN: Final = "open"
_CONVERSATION_OPEN: Final = "open"


def queue_expert_notice(
    outbox: Outbox,
    clock: Clock,
    destination: Destination,
    text: str,
    *,
    key: str,
    conversation_id: int | None = None,
) -> None:
    outbox.queue(
        None,
        Purpose.EXPERT,
        [destination],
        Message(text),
        key=key,
        now=clock.now(),
        conversation_id=conversation_id,
    )


class ExpertTurns:
    """Run the turns of Expert conversations and answer the commands of their topics.

    The turns of one conversation run one at a time, oldest first: each takes the conversation's
    lock before any await, and asyncio wakes lock waiters in order, so the order of arrival holds.
    A turn is journaled before it waits, and only a pending turn starts, so a repeated delivery
    never runs a turn twice. A turn that could not start stays pending, and the turns after it
    wait for its retry. Commands run without the lock: `/effort` takes effect from the next turn
    that starts. A conversation that is ending starts no turn; its endings are in `ExpertEnding`.
    """

    def __init__(
        self,
        log: ConversationLog,
        outbox: Outbox,
        clock: Clock,
        *,
        expert: Expert,
        topics: ExpertTopics,
        clients: Mapping[str, ShikiruClient],
        scope: Mapping[str, Path],
        directory: Path,
        channel: str,
        group_id: int,
        runs: ConversationRuns,
        ending: ExpertEnding,
        proposals: ExpertProposals,
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
        self._runs = runs
        self._ending = ending
        self._proposals = proposals

    def owns(self, item: Inbound) -> bool:
        """Return whether a text, reply or closed topic belongs to an open Expert topic."""
        return (
            item.kind in (InboundKind.TEXT, InboundKind.REPLY, InboundKind.TOPIC_CLOSED)
            and item.chat == self._group_id
            and item.thread is not None
            and self._log.at_thread(item.thread) is not None
        )

    async def handle(self, item: Inbound) -> None:
        conversation = (
            None
            if item.chat != self._group_id or item.thread is None
            else self._log.at_thread(item.thread)
        )
        if conversation is None:
            return
        if item.kind is InboundKind.TOPIC_CLOSED:
            await self._ending.end(conversation.id)
            return
        self._log.touch(conversation.id, self._clock.now())
        text = item.text or ""
        words = text.split()
        if not text.startswith("/"):
            self._log.add_turn(conversation.id, item.update_id, text, self._clock.now())
            await self.run(conversation.id, item.update_id)
            return
        name = words[0].split("@", 1)[0].lower()
        if name == _EFFORT:
            self._effort(item, conversation, words[1:])
        elif name == _SUMMARIZE:
            self._log.add_turn(conversation.id, item.update_id, SUMMARY_PROMPT, self._clock.now())
            await self.run(conversation.id, item.update_id)
        elif name == _CLOSE:
            await self._ending.end(conversation.id)
        else:
            self._reply(item, _HELP, conversation.id)

    async def run(self, conversation_id: int, update_id: int) -> None:
        """Run the turns before this one that are pending, then this one, and send their answers.

        A turn that is already done only sends its answer again, which the outbox keeps once.
        """
        async with self._runs.lock(conversation_id):
            while (turn := self._log.oldest_pending(conversation_id)) is not None:
                if turn.update_id >= update_id:
                    break
                if not await self._execute(conversation_id, turn.update_id):
                    return
                await self._deliver(conversation_id, turn.update_id)
            own = self._log.turn(update_id)
            awaits_run = own is not None and own.state == _PENDING
            if awaits_run and not await self._execute(conversation_id, update_id):
                return
            await self._deliver(conversation_id, update_id)

    async def _execute(self, conversation_id: int, update_id: int) -> bool:
        """Run one pending turn in a task of its own; False when it did not run because it ends."""
        conversation = self._log.conversation(conversation_id)
        if conversation is None or conversation.state != _CONVERSATION_OPEN:
            return False
        # keep: its own task, so an ending stops the turn, not the message task that awaits it
        running = asyncio.create_task(self._answer(conversation, update_id))
        self._runs.track(conversation_id, running)
        try:
            await asyncio.wait({running})
        finally:
            self._runs.untrack(conversation_id)
            running.cancel()
        if running.cancelled():
            return False
        return running.result()

    async def _answer(self, conversation: ConversationRecord, update_id: int) -> bool:
        """Gather context, run the turn and journal its answer; False if it did not start."""
        turn = self._log.turn(update_id)
        if turn is None:
            return False
        context = await self._context(conversation.scope)
        if not self._log.start(update_id):
            return False
        directory = conversation_directory(self._directory, conversation.id)
        directory.mkdir(parents=True, exist_ok=True)
        request = ExpertRequest(
            directory=directory,
            scope={name: self._scope[name] for name in conversation.scope},
            message=turn.text,
            context=json.dumps(context),
            effort=conversation.effort,
            session=conversation.session,
            turns=tuple(
                ExpertTurn(
                    message=past.text,
                    reply=ExpertAnswer.model_validate_json(past.answer).reply,
                )
                for past in self._log.history(conversation.id, before=update_id)
                if past.answer is not None
            ),
        )
        try:
            result = await self._expert.turn(request)
        except ExpertError:
            answer = ExpertAnswer(reply=_NO_ANSWER, title="", tasks=[], findings="")
            self._log.finish(update_id, answer.model_dump_json(), None, None, self._clock.now())
        else:
            self._log.finish(
                update_id,
                result.answer.model_dump_json(),
                json.dumps(asdict(result.session)),
                None if result.call is None else json.dumps(asdict(result.call)),
                self._clock.now(),
            )
        return True

    async def _deliver(self, conversation_id: int, update_id: int) -> None:
        """Queue the answer of a done turn, and name the topic after the first answer."""
        conversation = self._log.conversation(conversation_id)
        turn = self._log.turn(update_id)
        if conversation is None or conversation.thread_id is None or turn is None:
            return
        if turn.state != _DONE or turn.answer is None:
            return
        answer = ExpertAnswer.model_validate_json(turn.answer)
        self._proposals.queue_answer(update_id)
        if answer.title and not self._log.history(conversation_id, before=update_id):
            with suppress(ChannelRefusedError):
                await self._topics.rename(conversation.thread_id, f"💬 {answer.title}")

    async def _context(self, scope: list[str]) -> dict[str, object]:
        context: dict[str, object] = {}
        for name in scope:
            client = self._clients[name]
            tasks = await client.list_tasks(list(TaskStatus.ACTIVE))
            escalations = await client.list_escalations()
            context[name] = {
                "tasks": [task.model_dump() for task in tasks],
                "escalations": [
                    view.model_dump()
                    for view in escalations
                    if view.escalation.status == _ESCALATION_OPEN
                ],
            }
        return context

    def _effort(self, item: Inbound, conversation: ConversationRecord, args: list[str]) -> None:
        if not args:
            reply = f"Effort: {conversation.effort or 'the default of each rung'}."
        elif len(args) == 1 and not args[0].startswith("-"):
            self._log.set_effort(conversation.id, args[0])
            reply = f"Effort set to {args[0]}. It applies from the next turn."
        else:
            reply = _EFFORT_USAGE
        self._reply(item, reply, conversation.id)

    def _reply(self, item: Inbound, text: str, conversation_id: int) -> None:
        queue_command_reply(
            self._outbox,
            [item_destination(self._channel, item)],
            text,
            key=f"expert:command:{item.update_id}",
            now=self._clock.now(),
            conversation_id=conversation_id,
        )

    def recover(self) -> None:
        """Tell topics about creations left uncertain and turns a restart interrupted."""
        self._proposals.recover()
        for row in self._log.recover_creations():
            queue_expert_notice(
                self._outbox,
                self._clock,
                Destination(self._channel, str(row.chat), row.origin_thread),
                CREATION_UNCERTAIN,
                key=f"ask:creation:{row.update_id}",
                conversation_id=row.id,
            )
        for turn in self._log.recover_turns():
            queue_expert_notice(
                self._outbox,
                self._clock,
                Destination(self._channel, str(self._group_id), turn.thread_id),
                _INTERRUPTED,
                key=f"ask:interrupted:{turn.update_id}",
                conversation_id=turn.conversation_id,
            )
