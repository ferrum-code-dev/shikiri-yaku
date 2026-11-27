"""A small channel fake with opaque place, room and message references."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.inbound import Batch, Received
from shikiri_yaku.domain.place import Place
from shikiri_yaku.ports import Channel, ChannelRefusedError, CheckLine, Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.signal import Kind

CHANNEL_NAME = "sample"
MAIN_PLACE = Place(CHANNEL_NAME, "place:main")
OWNER = "owner"
TEXT_LIMIT = 2000
BUTTON_DATA_LIMIT = 64
FILE_LIMIT = 1_000_000
_TEXT_LIMIT_REFUSAL = "message exceeds the channel text limit"
_BUTTON_LIMIT_REFUSAL = "button data exceeds the channel limit"
_UNOWNED_MESSAGE_REFUSAL = "message was not sent by this channel"
_UNOWNED_ROOM_REFUSAL = "room was not opened by this channel"


class _Inbox:
    def __init__(self) -> None:
        self.events: list[Received] = []
        self._positions: dict[str, int] = {}

    def add(self, event: Received) -> None:
        self.events.append(event)

    async def fetch(self, cursor: str | None, wait_s: float) -> Batch:
        del wait_s
        start = 0 if cursor is None else self._positions[cursor]
        if start == len(self.events):
            return Batch(cursor, ())
        next_cursor = f"cursor/after/{len(self.events)}"
        self._positions[next_cursor] = len(self.events)
        return Batch(next_cursor, tuple(self.events[start:]))


class _Transport:
    def __init__(self) -> None:
        self.sent: dict[str, tuple[Place, Message]] = {}
        self.deleted: set[str] = set()
        self.edited: list[str] = []
        self._next_ref = 0

    async def send(self, place: Place, message: Message) -> str:
        if len(message.shown()) > TEXT_LIMIT:
            raise ChannelRefusedError(_TEXT_LIMIT_REFUSAL)
        if any(len(button.data.encode("utf-8")) > BUTTON_DATA_LIMIT for button in message.buttons):
            raise ChannelRefusedError(_BUTTON_LIMIT_REFUSAL)
        self._next_ref += 1
        reference = f"message/ref/opaque-{self._next_ref}"
        self.sent[reference] = (place, message)
        return reference

    async def edit(self, message_ref: str, message: Message) -> None:
        if message_ref not in self.sent or message_ref in self.deleted:
            raise ChannelRefusedError(_UNOWNED_MESSAGE_REFUSAL)
        self.sent[message_ref] = (self.sent[message_ref][0], message)
        self.edited.append(message_ref)

    async def delete(self, message_ref: str) -> None:
        if message_ref not in self.sent or message_ref in self.deleted:
            raise ChannelRefusedError(_UNOWNED_MESSAGE_REFUSAL)
        self.deleted.add(message_ref)


class _Places:
    def for_signal(self, project: str, kind: Kind) -> Sequence[Place]:
        del kind
        return (Place(CHANNEL_NAME, f"place:component:{project}"),)

    def component_of(self, place: Place) -> str | None:
        prefix = "place:component:"
        return place.id.removeprefix(prefix) if place.id.startswith(prefix) else None

    async def prepare(self) -> None:
        pass


class _Check:
    async def verify(self) -> None:
        pass

    async def report(self) -> list[CheckLine]:
        return [CheckLine("sample", "ready")]


class _Downloads:
    async def fetch(self, file_id: str) -> bytes:
        return file_id.encode()


class _Rooms:
    def __init__(self, transport: _Transport) -> None:
        self._transport = transport
        self._places: dict[Place, str] = {}
        self._next_room = 0
        self.deleted: list[str] = []

    async def open(self, title: str) -> str:
        del title
        self._next_room += 1
        room = f"room/opaque/alpha-{self._next_room}"
        self._places[self.place(room)] = room
        return room

    def place(self, room: str) -> Place:
        return Place(CHANNEL_NAME, f"place:conversation:{room}")

    def room_of(self, place: Place) -> str | None:
        return self._places.get(place)

    async def rename(self, room: str, title: str) -> None:
        del room, title

    async def delete(self, room: str) -> None:
        if room not in self._places.values():
            raise ChannelRefusedError(_UNOWNED_ROOM_REFUSAL)
        self.deleted.append(room)
        place = self.place(room)
        self._transport.deleted.update(
            reference
            for reference, (sent_place, _message) in self._transport.sent.items()
            if sent_place == place
        )

    def link(self, room: str) -> str | None:
        return f"sample://conversation/{room}"


class _Shared:
    def is_main(self, place: Place) -> bool:
        return place == MAIN_PLACE

    async def addressed(self, word: str) -> bool:
        _command, separator, instance = word.partition("@")
        return not separator or instance == CHANNEL_NAME

    async def command_label(self, command: str) -> str:
        return f"{command}@{CHANNEL_NAME}"


class GenericChannel:
    """Port implementations used only by channel contract tests."""

    def __init__(self, *, rooms: bool = True, shared: bool = True) -> None:
        self.inbox = _Inbox()
        self.transport = _Transport()
        self.places = _Places()
        self.room_support = _Rooms(self.transport) if rooms else None
        self.shared_support = _Shared() if shared else None
        self.bundle = Channel(
            name=CHANNEL_NAME,
            inbox=self.inbox,
            sender=self.transport,
            editor=self.transport,
            deleter=self.transport,
            downloads=_Downloads(),
            places=self.places,
            check=_Check(),
            owners=frozenset({OWNER}),
            max_text=TEXT_LIMIT,
            max_file_bytes=FILE_LIMIT,
            secrets=(),
            rooms=self.room_support,
            shared=self.shared_support,
        )
