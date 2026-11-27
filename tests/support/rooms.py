"""Rooms and shared chat fakes: a room is a numbered thread of one group, as in Telegram."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.adapters.telegram import chat_and_thread, telegram_place

if TYPE_CHECKING:
    from shikiri_yaku.domain.place import Place

GROUP: Final = -100123
BOT: Final = "bot"


class FakeRooms:
    """Maps rooms to places; a test overrides each call it expects, and any other call fails."""

    def __init__(self, group_id: int = GROUP) -> None:
        self._group = group_id

    async def open(self, title: str) -> str:
        raise NotImplementedError

    def place(self, room: str) -> Place:
        return telegram_place(self._group, int(room))

    def room_of(self, place: Place) -> str | None:
        chat, thread = chat_and_thread(place)
        if chat != str(self._group) or thread is None:
            return None
        return str(thread)

    async def rename(self, room: str, title: str) -> None:
        raise NotImplementedError

    async def delete(self, room: str) -> None:
        raise NotImplementedError

    def link(self, room: str) -> str | None:
        return f"https://t.me/c/123/{room}"


class FakeSharedChat:
    """The instance is `BOT`; the main place is the one outside every thread."""

    def is_main(self, place: Place) -> bool:
        return chat_and_thread(place)[1] is None

    async def addressed(self, word: str) -> bool:
        _, _, instance = word.partition("@")
        return instance.lower() == BOT

    async def command_label(self, command: str) -> str:
        return f"{command}@{BOT}"
