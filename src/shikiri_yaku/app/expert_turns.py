"""Turns of an Expert conversation and the commands of its room."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Final
from unicodedata import category
from unicodedata import name as unicode_name

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.app.expert_files import conversation_file_names, save_attachment
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.domain.expert import (
    MODE_ADMIN,
    AdminAnswer,
    ExpertAnswer,
    ExpertRequest,
    ExpertTurn,
    admin_message_text,
    stored_answer,
)
from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import (
    ChannelRefusedError,
    ExpertError,
    Message,
    TaskStatus,
    join_blocks,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from shikiri_yaku.app.expert import ConversationLog
    from shikiri_yaku.app.expert_admin import AdminExecutor
    from shikiri_yaku.app.expert_ending import ExpertEnding
    from shikiri_yaku.app.expert_proposals import ExpertProposals
    from shikiri_yaku.app.expert_runs import ConversationRuns
    from shikiri_yaku.app.expert_sketches import ExpertSketches
    from shikiri_yaku.app.expert_web import ExpertWebRequests
    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.domain.inbound import FileRef, Inbound
    from shikiri_yaku.domain.place import Place
    from shikiri_yaku.ports import Clock, Downloads, Expert, Outbox, Rooms, ShikiruClient

CREATION_UNCERTAIN: Final = "Opening the conversation was uncertain. Please ask again."
SUMMARY_PROMPT: Final = "Summarize this conversation in two or three short sentences."
ADMIN_WARNING: Final = (
    "This chat is not private: others here can read this conversation, including any command "
    "the Expert proposes and its output."
)
_INTERRUPTED: Final = "The Expert turn was interrupted. Please ask again."
_NO_ANSWER: Final = "No Expert agent could answer."
_WITHHELD_REPLY: Final = "The answer was withheld by secret protection."
_EXPERT_FAILURE_REASON_LIMIT: Final = 240
_EXPERT_FAILURE_MESSAGE_LIMIT: Final = 1000
_EXPERT_FAILURE_LINE_MINIMUM: Final = 48
_EXPERT_FAILURE_OMISSION_RESERVE: Final = 32
_REFUSED_COMMAND: Final = "The proposed command could not be shown exactly and was refused."
_ATTACHED: Final = "Attached file (absolute path): "
_TOO_LARGE: Final = "The file is larger than the {limit} download limit, so it was not saved."
_MEGABYTE: Final = 1024 * 1024
_NOT_DOWNLOADED: Final = "The chat did not give the file, so it was not saved."
_NOT_SAVED: Final = "The file could not be saved in this conversation's files."
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
_DISPLAY_CONTROL_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Cn", "Zl", "Zp", "Zs"})
_DISPLAY_WHITESPACE: Final = " \n\t"
_INVISIBLE_NAMES: Final = (
    "VARIATION SELECTOR",
    "COMBINING GRAPHEME JOINER",
    "FILLER",
    "BRAILLE PATTERN BLANK",
)


def queue_expert_notice(
    outbox: Outbox,
    clock: Clock,
    place: Place,
    text: str,
    *,
    key: str,
    slot: str | None = None,
    conversation_id: int | None = None,
) -> None:
    outbox.queue(
        None,
        Purpose.EXPERT,
        [place],
        Message(text),
        key=key,
        now=clock.now(),
        slot=slot,
        conversation_id=conversation_id,
    )


class ExpertTurns:
    """Run the turns of Expert conversations and answer the commands of their rooms.

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
        rooms: Rooms,
        clients: Mapping[str, ShikiruClient],
        scope: Mapping[str, Path],
        directory: Path,
        max_text: int,
        max_file_bytes: int,
        runs: ConversationRuns,
        ending: ExpertEnding,
        proposals: ExpertProposals,
        downloads: Downloads,
        sketches: ExpertSketches,
        web: ExpertWebRequests | None = None,
        admins: frozenset[str] = frozenset(),
        known_secrets: tuple[str, ...] = (),
        admin_executor: AdminExecutor | None = None,
    ) -> None:
        self._log = log
        self._admin_executor = admin_executor
        self._downloads = downloads
        self._sketches = sketches
        self._outbox = outbox
        self._clock = clock
        self._expert = expert
        self._rooms = rooms
        self._clients = clients
        self._scope = scope
        self._directory = directory
        self._max_text = max_text
        self._max_file_bytes = max_file_bytes
        self._runs = runs
        self._ending = ending
        self._proposals = proposals
        self._web = web
        self._admins = admins
        self._known_secrets = tuple(secret for secret in known_secrets if secret)

    def owns(self, item: Inbound) -> bool:
        """Return whether a text, reply, file or closed room belongs to an open Expert room."""
        return (
            item.kind
            in (InboundKind.TEXT, InboundKind.REPLY, InboundKind.FILE, InboundKind.ROOM_CLOSED)
            and self._conversation_of(item) is not None
        )

    def _conversation_of(self, item: Inbound) -> ConversationRecord | None:
        room = self._rooms.room_of(item.place)
        return None if room is None else self._log.at_room(room)

    async def handle(self, item: Inbound) -> None:
        conversation = self._conversation_of(item)
        if conversation is None:
            return
        if item.kind is InboundKind.ROOM_CLOSED:
            await self._ending.end(conversation.id, sender=item.sender, event_id=item.id)
            return
        if conversation.mode == MODE_ADMIN and item.sender not in self._admins:
            return
        self._log.touch(conversation.id, self._clock.now())
        if item.file is not None:
            await self._attach(item, conversation, item.file)
            return
        text = item.text or ""
        command = item.command
        if command is None:
            self._log.add_turn(conversation.id, item.id, text, self._clock.now())
            await self.run(conversation.id, item.id)
            return
        if command.name == _EFFORT:
            self._effort(item, conversation, command.arguments.split())
        elif command.name == _SUMMARIZE:
            self._log.add_turn(conversation.id, item.id, SUMMARY_PROMPT, self._clock.now())
            await self.run(conversation.id, item.id)
        elif command.name == _CLOSE:
            await self._ending.end(conversation.id, sender=item.sender, event_id=item.id)
        else:
            self._reply(item, _HELP, conversation.id)

    async def _attach(
        self, item: Inbound, conversation: ConversationRecord, attached: FileRef
    ) -> None:
        """Save an owner's file, then make its turn: the caption and the file's absolute path.

        A file over the Bot API's limit, or one the Bot API will not give, gets a notice and no
        turn. A transient failure raises ChannelError, so the item stays pending and is tried again.
        """
        if attached.size is not None and attached.size > self._max_file_bytes:
            self._file_notice(
                item, conversation, _TOO_LARGE.format(limit=_size_text(self._max_file_bytes))
            )
            return
        # keep: locked from the download to the turn's end, so a later message waits behind the file
        async with self._runs.lock(conversation.id):
            try:
                content = await self._downloads.fetch(attached.id)
            except ChannelRefusedError:
                content = None
            # keep: the conversation may have ended meanwhile; nothing is written for it then
            if not self._is_open(conversation.id):
                return
            if content is None:
                self._file_notice(item, conversation, _NOT_DOWNLOADED)
                return
            try:
                path = save_attachment(
                    conversation_directory(self._directory, conversation.id),
                    item.id,
                    attached.name,
                    content,
                )
            except OSError:
                self._file_notice(item, conversation, _NOT_SAVED)
                return
            text = join_blocks(item.text or "", f"{_ATTACHED}{path}")
            self._log.add_turn(conversation.id, item.id, text, self._clock.now())
            await self._run_locked(conversation.id, item.id)

    def _is_open(self, conversation_id: int) -> bool:
        conversation = self._log.conversation(conversation_id)
        return conversation is not None and conversation.state == _CONVERSATION_OPEN

    def _file_notice(self, item: Inbound, conversation: ConversationRecord, text: str) -> None:
        if conversation.room is None:
            return
        queue_expert_notice(
            self._outbox,
            self._clock,
            self._rooms.place(conversation.room),
            text,
            key=f"expert:file:{item.id}",
            conversation_id=conversation.id,
        )

    def warn_admin(self, conversation: ConversationRecord) -> None:
        """Say in the conversation of an admin that the chat is not private."""
        if conversation.room is None:
            return
        queue_expert_notice(
            self._outbox,
            self._clock,
            self._rooms.place(conversation.room),
            ADMIN_WARNING,
            key=f"admin:warning:{conversation.event_id}",
            conversation_id=conversation.id,
        )

    async def run(self, conversation_id: int, event_id: int) -> None:
        """Run the turns before this one that are pending, then this one, and send their answers.

        A turn that is already done only sends its answer again, which the outbox keeps once.
        """
        async with self._runs.lock(conversation_id):
            await self._run_locked(conversation_id, event_id)

    async def _run_locked(self, conversation_id: int, event_id: int) -> None:
        while (turn := self._log.oldest_pending(conversation_id)) is not None:
            if turn.event_id < 0 or turn.event_id >= event_id:
                break
            if not await self._execute(conversation_id, turn.event_id):
                return
            await self._deliver(conversation_id, turn.event_id)
        own = self._log.turn(event_id)
        awaits_run = own is not None and own.state == _PENDING
        if awaits_run and not await self._execute(conversation_id, event_id):
            return
        await self._deliver(conversation_id, event_id)
        # keep: a generated turn may be done but undelivered after a stop, so each is delivered
        delivered: set[int] = set()
        while followup := next(
            (t for t in self._log.followups(conversation_id) if t.event_id not in delivered),
            None,
        ):
            if followup.state == _PENDING and not await self._execute(
                conversation_id, followup.event_id
            ):
                return
            await self._deliver(conversation_id, followup.event_id)
            delivered.add(followup.event_id)

    async def _run_admin(self, op_id: int, conversation_id: int) -> None:
        async with self._runs.lock(conversation_id):
            if self._admin_executor is not None:
                await self._admin_executor.execute(op_id, self._run_locked)

    async def _execute(self, conversation_id: int, event_id: int) -> bool:
        """Run one pending turn in a task of its own; False when it did not run because it ends."""
        conversation = self._log.conversation(conversation_id)
        if conversation is None or conversation.state != _CONVERSATION_OPEN:
            return False
        # keep: its own task, so an ending stops the turn, not the message task that awaits it
        running = asyncio.create_task(self._answer(conversation, event_id))
        self._runs.track(conversation_id, running)
        try:
            await asyncio.wait({running})
        finally:
            self._runs.untrack(conversation_id)
            running.cancel()
        if running.cancelled():
            return False
        return running.result()

    async def _answer(self, conversation: ConversationRecord, event_id: int) -> bool:
        """Gather context, run the turn and journal its answer; False if it did not start."""
        turn = self._log.turn(event_id)
        if turn is None:
            return False
        context = await self._context(conversation.scope)
        if not self._log.start(event_id):
            return False
        directory = conversation_directory(self._directory, conversation.id)
        directory.mkdir(parents=True, exist_ok=True)
        request = ExpertRequest(
            directory=directory,
            scope={name: self._scope[name] for name in conversation.scope},
            message=turn.text,
            files=conversation_file_names(directory),
            event_id=event_id,
            admin=conversation.mode == MODE_ADMIN,
            context=json.dumps(context),
            effort=conversation.effort,
            session=conversation.session,
            turns=tuple(
                ExpertTurn(
                    message=past.text,
                    reply=stored_answer(past.answer).reply,
                )
                for past in self._log.history(conversation.id, before=event_id)
                if past.answer is not None
            ),
        )
        try:
            result = await self._expert.turn(request)
        except ExpertError as error:
            answer = ExpertAnswer(
                reply=_expert_failure_reply(error, self._known_secrets),
                title="",
                tasks=[],
                findings="",
            )
            self._log.finish(event_id, answer.model_dump_json(), None, None, self._clock.now())
        else:
            stored = result.answer
            texts = (
                [
                    *_admin_texts(stored),
                    result.session.findings,
                    result.session.session_id,
                    result.session.provider,
                ]
                if isinstance(stored, AdminAnswer)
                else []
            )
            withheld = bool(texts) and (
                secret_form(texts) is not None
                or any(secret in text for secret in self._known_secrets for text in texts)
            )
            refused = (
                isinstance(stored, AdminAnswer)
                and stored.command is not None
                and not _showable_command(stored.command, stored.reason, self._max_text)
            )
            refused_proposal = (
                isinstance(stored, AdminAnswer)
                and stored.command is not None
                and (withheld or refused)
            )
            if withheld or refused:
                stored = AdminAnswer(
                    reply=_WITHHELD_REPLY if withheld else _REFUSED_COMMAND,
                    title="",
                    tasks=[],
                    findings="",
                    command=None,
                    reason="",
                )
            self._log.finish(
                event_id,
                stored.model_dump_json(),
                json.dumps(asdict(result.session)),
                None if result.call is None else json.dumps(asdict(result.call)),
                self._clock.now(),
                clear_session=withheld or refused,
                refused=refused_proposal,
            )
        return True

    async def _deliver(self, conversation_id: int, event_id: int) -> None:
        """Queue the answer of a done turn, and name the room after the first answer."""
        conversation = self._log.conversation(conversation_id)
        turn = self._log.turn(event_id)
        if conversation is None or conversation.room is None or turn is None:
            return
        if turn.state != _DONE or turn.answer is None:
            return
        answer = stored_answer(turn.answer)
        self._proposals.queue_answer(event_id)
        self._proposals.queue_admin(event_id)
        if self._web is not None:
            self._web.queue(event_id)
        if answer.title and not self._log.history(conversation_id, before=event_id):
            with suppress(ChannelRefusedError):
                await self._rooms.rename(conversation.room, f"💬 {answer.title}")
        if description := (answer.image or "").strip():
            await self._draw(conversation_id, event_id, description)

    async def _draw(self, conversation_id: int, event_id: int, description: str) -> None:
        """Draw an answer's sketch in a task an ending stops, unless the conversation is ending."""
        conversation = self._log.conversation(conversation_id)
        if conversation is None or conversation.state != _CONVERSATION_OPEN:
            return
        drawing = asyncio.create_task(self._sketches.draw(conversation, event_id, description))
        self._runs.track(conversation_id, drawing)
        try:
            await asyncio.wait({drawing})
        finally:
            self._runs.untrack(conversation_id)
            drawing.cancel()
        if not drawing.cancelled():
            drawing.result()

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

    async def web_tap(self, item: Inbound) -> bool:
        """Apply a web grant decision if this is a web request tap."""
        if await self._proposals.admin_tap(item, self.run, self._run_admin):
            return True
        return self._web is not None and await self._web.tap(item, self.run)

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
            [item.place],
            text,
            key=f"expert:command:{item.id}",
            now=self._clock.now(),
            conversation_id=conversation_id,
            max_text=self._max_text,
        )

    async def resume_admin_followups(self) -> None:
        """Resume generated admin turns left pending before a restart."""
        for conversation_id, event_id in self._log.pending_admin_followups():
            await self.run(conversation_id, event_id)

    def recover(self) -> None:
        """Tell rooms about creations left uncertain and turns a restart interrupted."""
        self._proposals.recover()
        for row in self._log.recover_creations():
            queue_expert_notice(
                self._outbox,
                self._clock,
                row.origin,
                CREATION_UNCERTAIN,
                key=f"expert:creation:{row.event_id}",
                conversation_id=row.id,
            )
        for turn in self._log.recover_turns():
            if turn.room is None:
                continue
            queue_expert_notice(
                self._outbox,
                self._clock,
                self._rooms.place(turn.room),
                _INTERRUPTED,
                key=f"expert:interrupted:{turn.event_id}",
                conversation_id=turn.conversation_id,
            )
        self._sketches.recover()
        if self._web is not None:
            self._web.queue()
            self._web.recover()


def _admin_texts(answer: AdminAnswer) -> list[str]:
    texts = [answer.reply, answer.title, answer.findings, answer.reason]
    texts.extend(
        text
        for task in answer.tasks
        for text in task.model_dump().values()
        if isinstance(text, str)
    )
    if answer.command is not None:
        texts.append(answer.command)
    return texts


def _expert_failure_reply(error: ExpertError, known_secrets: tuple[str, ...]) -> str:
    lines = [
        f"{name}: {text.removeprefix(f'{name}: ')}" if name else text
        for name, text in error.rung_failures or (("", str(error)),)
    ]
    if secret_form(lines) is not None or any(
        secret in line for secret in known_secrets for line in lines
    ):
        return _WITHHELD_REPLY
    budget = _EXPERT_FAILURE_MESSAGE_LIMIT - len(_NO_ANSWER) - _EXPERT_FAILURE_OMISSION_RESERVE
    shown = min(len(lines), budget // _EXPERT_FAILURE_LINE_MINIMUM)
    each = min(_EXPERT_FAILURE_REASON_LIMIT, budget // shown - len("\n"))
    text = _NO_ANSWER + "".join(f"\n{line[:each]}" for line in lines[:shown])
    if shown < len(lines):
        text += f"\n+{len(lines) - shown} more rungs not shown"
    return text


def _size_text(size: int) -> str:
    """Return a size the PO can compare with the file: whole megabytes, or else the exact bytes."""
    if size % _MEGABYTE == 0:
        return f"{size // _MEGABYTE} MB"
    return f"{size} bytes"


def _showable_command(command: str, reason: str, max_text: int) -> bool:
    text = admin_message_text(command, reason)
    return len(text) <= max_text and all(
        character in _DISPLAY_WHITESPACE
        or (
            category(character) not in _DISPLAY_CONTROL_CATEGORIES
            and not any(part in unicode_name(character, "") for part in _INVISIBLE_NAMES)
        )
        for character in text
    )
