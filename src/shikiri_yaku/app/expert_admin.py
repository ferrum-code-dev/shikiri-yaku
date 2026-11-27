"""Who may act in an admin conversation: the configured admin list, checked on every action."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol

from shikiri_yaku.domain.expert import MODE_ADMIN
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.app.expert import ConversationLog
    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.ports import (
        AdminCommandResult,
        AdminCommandRunner,
        Clock,
        HostLock,
        Outbox,
    )

_CUT_NOTE = " Cut output."
_WHOLE_NOTE = " All output."


class AdminConversations(Protocol):
    """The conversations an admin check reads and keeps active."""

    def conversation(self, conversation_id: int) -> ConversationRecord | None: ...
    def touch(self, conversation_id: int, now: datetime) -> None: ...


class AdminAccess:
    """Admit a sender: anyone to an Expert conversation, only a listed admin to an admin one.

    An admitted action in an admin conversation counts as activity, so the idle period restarts.
    """

    def __init__(self, log: AdminConversations, clock: Clock, *, admins: frozenset[int]) -> None:
        self._log = log
        self._clock = clock
        self._admins = admins

    def is_admin(self, sender: int) -> bool:
        return sender in self._admins

    def admit(self, conversation_id: int, sender: int) -> bool:
        conversation = self._log.conversation(conversation_id)
        if conversation is None or conversation.mode != MODE_ADMIN:
            return True
        if sender not in self._admins:
            return False
        self._log.touch(conversation_id, self._clock.now())
        return True


class AdminLog(Protocol):
    """The guarded audit transitions for admin execution."""

    def issue_admin(self, op_id: int, now: datetime) -> tuple[int, str] | None: ...
    def finish_admin(
        self,
        op_id: int,
        now: datetime,
        output: str,
        code: int | None,
        duration: float,
        *,
        cut: bool,
        notice: Callable[[], None] | None = None,
    ) -> int | None: ...
    def recover_admin(
        self,
        now: datetime,
        notice: Callable[[int, int, str], None] | None = None,
    ) -> list[tuple[int, int, str]]: ...


class AdminExecutor:
    """Run a claimed admin command once under the shared host lock."""

    def __init__(  # noqa: PLR0917 -- distinct ports
        self,
        log: AdminLog,
        conversations: ConversationLog,
        commands: AdminCommandRunner,
        host: HostLock,
        outbox: Outbox,
        clock: Clock,
        *,
        directory: Path,
        shell: str,
        timeout_s: float,
        max_bytes: int,
        secrets: Sequence[str],
        channel: str,
        group_id: int,
    ) -> None:
        self._log = log
        self._conversations = conversations
        self._commands = commands
        self._host = host
        self._outbox = outbox
        self._clock = clock
        self._directory = directory
        self._shell = shell
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._secrets = secrets
        self._channel = channel
        self._group_id = group_id

    async def execute(self, op_id: int, run: Callable[[int, int], Awaitable[None]]) -> None:
        async with self._host.hold():
            started = self._clock.now()
            claim = self._log.issue_admin(op_id, started)
            if claim is None:
                return
            conversation_id, command = claim
            result = await self._commands.run_admin(
                [self._shell, "-l", "-c", command],
                timeout_s=self._timeout_s,
                cwd=self._directory,
                max_bytes=self._max_bytes,
                secrets=self._secrets,
            )
            now = self._clock.now()
            duration = (now - started).total_seconds()
            text = _result_text(result, duration)
            conversation = self._conversations.conversation(conversation_id)
            thread = None if conversation is None else conversation.thread_id
            notice = (
                None
                if thread is None
                else partial(self._notice, op_id, conversation_id, thread, text)
            )
            turn = self._log.finish_admin(
                op_id,
                now,
                text,
                result.returncode,
                duration,
                cut=result.cut,
                notice=notice,
            )
        if turn is not None:
            await run(conversation_id, turn)

    def _notice(self, op_id: int, conversation_id: int, thread: int, text: str) -> None:
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [Destination(self._channel, str(self._group_id), thread)],
            Message(text),
            key=f"admin:result:{op_id}",
            now=self._clock.now(),
            conversation_id=conversation_id,
        )


def _result_text(result: AdminCommandResult, duration: float) -> str:
    if result.returncode is None:
        exit_code = "Exit code unavailable: killed at timeout or did not start"
    else:
        exit_code = f"Exit code {result.returncode}"
    head = f"{exit_code} after {duration:.1f} s."
    body = result.output or "(no output)"
    # keep: both notes have the same length, so the tail budget does not depend on the choice
    cut = result.cut or len(head) + len(_CUT_NOTE) + 1 + len(body) > MESSAGE_TEXT_LIMIT
    status = head + (_CUT_NOTE if cut else _WHOLE_NOTE)
    return f"{status}\n{body[-(MESSAGE_TEXT_LIMIT - len(status) - 1) :]}"


def recover_admin(
    log: AdminLog,
    conversations: AdminConversations,
    clock: Clock,
    outbox: Outbox,
    *,
    channel_name: str,
    group_id: int | None,
) -> None:
    """Settle interrupted operations even if admin mode is no longer configured.

    Each notice is queued in the transaction that settles its operation, so a restart cannot lose
    it; the outbox sender delivers it, and tries again after a failed delivery.
    """

    def notice(op_id: int, conversation_id: int, state: str) -> None:
        conversation = conversations.conversation(conversation_id)
        if conversation is None or conversation.thread_id is None or group_id is None:
            return
        outbox.queue(
            None,
            Purpose.EXPERT,
            [Destination(channel_name, str(group_id), conversation.thread_id)],
            Message(f"Command is {state} after restart. Please check the host."),
            key=f"admin:interrupted:{op_id}",
            now=clock.now(),
            conversation_id=conversation_id,
        )

    log.recover_admin(clock.now(), notice)
