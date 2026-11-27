"""Open an Expert conversation: journal the request, create its topic, then run its first turn."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.app.expert_turns import CREATION_UNCERTAIN, queue_expert_notice
from shikiri_yaku.domain.callback import EXPERT_SELECTION_PREFIX
from shikiri_yaku.domain.expert import MODE_ADMIN, MODE_EXPERT
from shikiri_yaku.domain.outbound import TIME_FORMAT
from shikiri_yaku.ports import Button, ChannelError, Message

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.app.expert_turns import ExpertTurns
    from shikiri_yaku.domain.expert import ConversationRecord, TurnRecord
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, FinalEdits, Outbox, Rooms, SharedChat

_OPEN: Final = "open"
OPEN_REPLY: Final = "Open the Expert conversation."
_CLOSED: Final = "Conversation closed {when}"
_CLOSED_TIME: Final = f"%Y-%m-%d {TIME_FORMAT}"
ADMIN_COMMAND: Final = "/admin"
ADMIN_OPEN_REPLY: Final = "Open the admin conversation."
_ADMIN_NOT_CONFIGURED: Final = "Admin mode is not configured."
_ADMIN_ONLY: Final = "Only a listed admin may use /admin."


def open_reply_keys(update_id: int) -> tuple[str, str]:
    """Return the notice keys of a conversation's /expert reply: the open and the selection one."""
    return f"expert:open:{update_id}:0", f"expert:select:{update_id}:0"


def closed_reply(closed_at: datetime) -> Message:
    """Return the /expert reply of an ended conversation: its text shows the time, and no button."""
    return Message(_CLOSED.format(when=closed_at.astimezone(UTC).strftime(_CLOSED_TIME)))


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
        mode: str = MODE_EXPERT,
    ) -> bool: ...
    def get(self, update_id: int) -> ConversationRecord | None: ...
    def conversation(self, conversation_id: int) -> ConversationRecord | None: ...
    def at_room(self, room: str) -> ConversationRecord | None: ...
    def turn(self, update_id: int) -> TurnRecord | None: ...
    def oldest_pending(self, conversation_id: int) -> TurnRecord | None: ...
    def followups(self, conversation_id: int) -> list[TurnRecord]: ...
    def pending_admin_followups(self) -> list[tuple[int, int]]: ...
    def history(self, conversation_id: int, *, before: int) -> list[TurnRecord]: ...
    def add_turn(self, conversation_id: int, update_id: int, text: str, now: datetime) -> bool: ...
    def refuse_admin(self, conversation_id: int, update_id: int, now: datetime) -> int | None: ...
    def set_effort(self, conversation_id: int, effort: str) -> None: ...
    def fail(self, update_id: int) -> None: ...
    def opened(self, update_id: int, room: str) -> None: ...
    def start(self, update_id: int) -> bool: ...
    def finish(
        self,
        update_id: int,
        answer: str,
        session: str | None,
        call: str | None,
        now: datetime,
        *,
        clear_session: bool = False,
        refused: bool = False,
    ) -> None: ...
    def touch(self, conversation_id: int, now: datetime) -> None: ...
    def recover_creations(self) -> list[ConversationRecord]: ...
    def recover_turns(self) -> list[TurnRecord]: ...


class Ask:
    """Journal a request before creating its topic and starting its first turn."""

    def __init__(
        self,
        log: ConversationLog,
        outbox: Outbox,
        clock: Clock,
        *,
        configured: bool,
        rooms: Rooms | None,
        shared: SharedChat | None = None,
        turns: ExpertTurns | None,
        scope: Mapping[str, Path],
        channel: str,
        max_text: int,
        final: FinalEdits | None = None,
        admins: frozenset[int] = frozenset(),
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._configured = configured
        self._rooms = rooms
        self._shared = shared
        self._turns = turns
        self._scope = scope
        self._channel = channel
        self._max_text = max_text
        self._final = final
        self._admins = admins

    def is_admin(self, sender: int) -> bool:
        return sender in self._admins

    async def offer(self, item: Inbound) -> None:
        if not self._configured or self._rooms is None or self._shared is None:
            return
        text = _question(item)
        if _is_admin(item) and item.sender not in self._admins:
            return
        reason = self._refusal(item, text)
        if reason:
            await self.handle(item, None)
            return
        label = await self._shared.command_label(ADMIN_COMMAND if _is_admin(item) else "/expert")
        self._outbox.queue_command_reply(
            [item_destination(self._channel, item)],
            [
                Message(
                    "Choose an Expert.",
                    (Button(label, f"{EXPERT_SELECTION_PREFIX}{item.update_id}"),),
                    reply_to=item.message_ref,
                )
            ],
            key=f"expert:select:{item.update_id}",
            now=self._clock.now(),
        )

    async def tap(self, item: Inbound) -> bool:
        original = self._outbox.expert_selection(self._channel, item)
        if original is None:
            return False
        if _is_admin(original) and item.sender not in self._admins:
            return True
        await self.handle(original, None, selection=True)
        return True

    async def handle(
        self, item: Inbound, component: str | None, *, selection: bool = False
    ) -> None:
        text = _question(item)
        reason = self._refusal(item, text)
        if reason:
            queue_command_reply(
                self._outbox,
                [item_destination(self._channel, item)],
                reason,
                key=f"expert:refusal:{item.update_id}",
                now=self._clock.now(),
                max_text=self._max_text,
            )
            return
        if self._rooms is None or self._turns is None:
            return
        row = await self._open(self._rooms, item, component, text)
        if row is None or row.state != _OPEN or row.room is None:
            return
        url = self._rooms.link(row.room)
        buttons = () if url is None else (Button("Open topic", url=url),)
        admin = row.mode == MODE_ADMIN
        message = Message(ADMIN_OPEN_REPLY if admin else OPEN_REPLY, buttons)
        if selection:
            if self._final is not None:
                _, selection_key = open_reply_keys(item.update_id)
                self._final.settle_notice(selection_key, message, conversation_id=row.id)
        else:
            self._outbox.queue_command_reply(
                [item_destination(self._channel, item)],
                [Message(message.text, message.buttons, reply_to=item.message_ref)],
                key=f"expert:open:{item.update_id}",
                now=self._clock.now(),
                conversation_id=row.id,
            )
        if admin:
            self._turns.warn_admin(row)
        await self._turns.run(row.id, item.update_id)

    async def _open(
        self, rooms: Rooms, item: Inbound, component: str | None, text: str
    ) -> ConversationRecord | None:
        names = [component] if component is not None else list(self._scope)
        created = self._log.create(
            item.update_id,
            chat=item.chat,
            thread=item.thread,
            text=text,
            scope=names,
            now=self._clock.now(),
            mode=MODE_ADMIN if _is_admin(item) else MODE_EXPERT,
        )
        if created:
            try:
                room = await rooms.open(f"💬 {text}")
            except ChannelError:
                self._log.fail(item.update_id)
                queue_expert_notice(
                    self._outbox,
                    self._clock,
                    item_destination(self._channel, item),
                    CREATION_UNCERTAIN,
                    key=f"expert:creation:{item.update_id}",
                )
                return None
            self._log.opened(item.update_id, room)
        return self._log.get(item.update_id)

    def _refusal(self, item: Inbound, text: str) -> str:
        if not self._configured:
            return "The Expert is not configured."
        if self._rooms is None:
            return "The Expert requires a forum group."
        if _is_admin(item):
            if not self._admins:
                return _ADMIN_NOT_CONFIGURED
            if item.sender not in self._admins:
                return _ADMIN_ONLY
            return "Provide a question after /admin." if not text else ""
        return "Provide a question after /expert." if not text else ""


def _is_admin(item: Inbound) -> bool:
    command = item.command
    return command is not None and command.name == ADMIN_COMMAND


def _question(item: Inbound) -> str:
    command = item.command
    return "" if command is None else command.arguments
