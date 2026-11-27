"""Rooms and shared chat fakes: a room is a numbered thread of one group, as in Telegram."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.domain.schedule import Destination

GROUP: Final = -100123
BOT: Final = "bot"


class FakeRooms:
    """Maps rooms to places; a test overrides each call it expects, and any other call fails."""

    def __init__(self, group_id: int = GROUP) -> None:
        self._group = str(group_id)

    async def open(self, title: str) -> str:
        raise NotImplementedError

    def place(self, room: str) -> Destination:
        return Destination("telegram", self._group, int(room))

    def room_of(self, place: Destination) -> str | None:
        if place.chat != self._group or place.thread is None:
            return None
        return str(place.thread)

    async def rename(self, room: str, title: str) -> None:
        raise NotImplementedError

    async def delete(self, room: str) -> None:
        raise NotImplementedError

    def link(self, room: str) -> str | None:
        return f"https://t.me/c/123/{room}"


class FakeSharedChat:
    """The instance is `BOT`; the main place is the one outside every thread."""

    def is_main(self, place: Destination) -> bool:
        return place.thread is None

    async def addressed(self, word: str) -> bool:
        _, _, instance = word.partition("@")
        return instance.lower() == BOT

    async def command_label(self, command: str) -> str:
        return f"{command}@{BOT}"
