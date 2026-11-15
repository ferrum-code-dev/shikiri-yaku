"""Who may act in an admin conversation: the configured admin list, checked on every action."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from shikiri_yaku.domain.expert import MODE_ADMIN

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.ports import Clock


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
