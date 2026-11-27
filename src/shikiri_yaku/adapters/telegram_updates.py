"""The PO's side of the Telegram chat: `getUpdates` over the Bot API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

from shikiri_yaku.adapters.telegram import (
    REF_SEPARATOR,
    UNEXPECTED_REPLY,
    chat_and_thread,
    telegram_place,
)
from shikiri_yaku.domain.inbound import Batch, FileRef, InboundKind, Received
from shikiri_yaku.ports import ChannelProtocolError

if TYPE_CHECKING:
    from shikiri_yaku.adapters.telegram import BotApi

GET_UPDATES: Final = "getUpdates"
ALLOWED_UPDATES: Final = ("message", "callback_query")
# keep: Telegram's built-in General topic is 1 on the wire; the app knows it as no thread
GENERAL_THREAD: Final = 1
# keep: the Bot API names a document it sends without a name, and a photo carries none
DOCUMENT_NAME: Final = "document"
PHOTO_NAME: Final = "photo.jpg"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Identity(_Payload):
    id: int


class _Replied(_Payload):
    message_id: int


class _Closed(_Payload):
    """The service message Telegram sends when a forum topic is closed; it has no fields we read."""


class _Sized(_Payload):
    file_id: str
    file_size: StrictInt | None = None


class _Document(_Sized):
    file_name: str | None = None


class _Message(_Payload):
    message_id: int
    sender: Annotated[_Identity, Field(alias="from")]
    chat: _Identity
    text: str | None = None
    caption: str | None = None
    photo: list[_Sized] | None = None
    document: _Document | None = None
    message_thread_id: StrictInt | None = None
    reply_to_message: _Replied | None = None
    forum_topic_closed: _Closed | None = None


class _Tapped(_Payload):
    message_id: int
    chat: _Identity
    message_thread_id: StrictInt | None = None
    is_topic_message: bool = False


class _Callback(_Payload):
    sender: Annotated[_Identity, Field(alias="from")]
    message: _Tapped
    data: str


class _Update(_Payload):
    update_id: StrictInt
    message: _Message | None = None
    callback_query: _Callback | None = None


class _Result(_Payload):
    ok: Literal[True]
    result: list[_Update]


class _Refused(_Payload):
    ok: Literal[False]
    error_code: StrictInt
    description: str


_REPLY: Final[TypeAdapter[_Result | _Refused]] = TypeAdapter(_Result | _Refused)


class TelegramUpdates:
    """Long-polls `getUpdates` and reads each update into a Received event.

    The cursor is the last update id plus one, which `getUpdates` takes as its offset.

    Unsupported kinds and messages without text give no event but move the cursor. So does
    an event from a chat that is neither a private chat nor the configured group.
    Invalid supported updates fail the whole batch without exposing their payload in errors.
    """

    def __init__(self, api: BotApi, *, group_id: int | None) -> None:
        self._api = api
        self._group_id = group_id

    async def fetch(self, cursor: str | None, wait_s: float) -> Batch:
        offset = 0 if cursor is None else int(cursor)
        body = await self._api.call(
            GET_UPDATES,
            {"offset": offset, "timeout": wait_s, "allowed_updates": list(ALLOWED_UPDATES)},
        )
        try:
            reply = _REPLY.validate_python(body)
        except ValidationError:
            raise ChannelProtocolError(UNEXPECTED_REPLY) from None
        if isinstance(reply, _Refused):
            raise ChannelProtocolError(_refusal(reply))
        events: list[Received] = []
        last: int | None = None
        for update in reply.result:
            last = update.update_id if last is None else max(last, update.update_id)
            if (event := _event(update)) is not None and self._served_chat(event):
                events.append(event)
        return Batch(None if last is None else str(last + 1), tuple(events))

    def _served_chat(self, event: Received) -> bool:
        chat, _ = chat_and_thread(event.place)
        return chat in (str(self._group_id), event.sender)


def _event(update: _Update) -> Received | None:
    if update.callback_query is not None:
        tap = update.callback_query
        tapped = tap.message
        # keep: outside a topic, a thread names a chain of replies, which is no place
        thread = _thread(tapped.message_thread_id) if tapped.is_topic_message else None
        return Received(
            key=str(update.update_id),
            kind=InboundKind.TAP,
            sender=str(tap.sender.id),
            place=telegram_place(tapped.chat.id, thread),
            message_ref=_ref(tapped.chat.id, tapped.message_id),
            data=tap.data,
        )
    if update.message is not None and update.message.forum_topic_closed is not None:
        closed = update.message
        return Received(
            key=str(update.update_id),
            kind=InboundKind.ROOM_CLOSED,
            sender=str(closed.sender.id),
            place=telegram_place(closed.chat.id, _thread(closed.message_thread_id)),
            message_ref=_ref(closed.chat.id, closed.message_id),
        )
    if update.message is not None and (attached := _file(update.message)) is not None:
        sent = update.message
        return Received(
            key=str(update.update_id),
            kind=InboundKind.FILE,
            sender=str(sent.sender.id),
            place=telegram_place(sent.chat.id, _thread(sent.message_thread_id)),
            message_ref=_ref(sent.chat.id, sent.message_id),
            text=sent.caption,
            file=attached,
        )
    if update.message is not None and update.message.text is not None:
        sent = update.message
        replied = _replied(sent)
        return Received(
            key=str(update.update_id),
            kind=InboundKind.TEXT if replied is None else InboundKind.REPLY,
            sender=str(sent.sender.id),
            place=telegram_place(sent.chat.id, _thread(sent.message_thread_id)),
            message_ref=_ref(sent.chat.id, sent.message_id),
            reply_to_ref=None if replied is None else _ref(sent.chat.id, replied.message_id),
            text=sent.text,
        )
    return None


def _file(sent: _Message) -> FileRef | None:
    """Return the document or the largest photo size a message sends, None when it sends neither."""
    if sent.document is not None:
        document = sent.document
        name = document.file_name or DOCUMENT_NAME
        return FileRef(id=document.file_id, name=name, size=document.file_size)
    if sent.photo:
        # keep: Telegram lists the sizes of a photo from the smallest to the largest
        largest = sent.photo[-1]
        return FileRef(id=largest.file_id, name=PHOTO_NAME, size=largest.file_size)
    return None


def _thread(thread: int | None) -> int | None:
    """Return the topic a message names, with the built-in General topic as no thread."""
    return None if thread == GENERAL_THREAD else thread


def _replied(sent: _Message) -> _Replied | None:
    """Return the message a reply answers, or None when the message is no reply."""
    replied = sent.reply_to_message
    # keep: a forum topic's messages name its first message; that is the topic, not a reply
    if replied is None or replied.message_id == sent.message_thread_id:
        return None
    return replied


def _refusal(reply: _Refused) -> str:
    # keep: the description is Telegram's own text and says why (a webhook conflict, a bad token)
    return f"{GET_UPDATES} was refused with {reply.error_code}: {reply.description}"


def _ref(chat: int, message_id: int) -> str:
    return f"{chat}{REF_SEPARATOR}{message_id}"
