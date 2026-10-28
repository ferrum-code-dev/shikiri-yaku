"""The PO's side of the Telegram chat: `getUpdates` over the Bot API (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

from shikiri_yaku.adapters.telegram import REF_SEPARATOR, UNEXPECTED_REPLY
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from shikiri_yaku.adapters.telegram import BotApi

GET_UPDATES: Final = "getUpdates"
ALLOWED_UPDATES: Final = ("message", "callback_query")
NOT_ACCEPTED: Final = "the Bot API did not accept getUpdates"


class _Payload(BaseModel):
    """Only the fields the Operator reads; a field Telegram adds is ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _Sender(_Payload):
    id: int


class _Chat(_Payload):
    id: int


class _Replied(_Payload):
    message_id: int


class _Message(_Payload):
    message_id: int
    sender: Annotated[_Sender, Field(alias="from")]
    chat: _Chat
    text: str | None = None
    reply_to_message: _Replied | None = None


class _Tapped(_Payload):
    message_id: int
    chat: _Chat


class _Callback(_Payload):
    sender: Annotated[_Sender, Field(alias="from")]
    message: _Tapped
    data: str


class _Identified(_Payload):
    update_id: StrictInt


class _Update(_Identified):
    message: _Message | None = None
    callback_query: _Callback | None = None


class _Failed(_Payload):
    ok: Literal[False]


class _Result(_Payload):
    ok: Literal[True]
    result: list[Any]


_RESULT: Final[TypeAdapter[_Result]] = TypeAdapter(_Result)
_UPDATE: Final[TypeAdapter[_Update]] = TypeAdapter(_Update)
_FAILED: Final[TypeAdapter[_Failed]] = TypeAdapter(_Failed)
_IDENTIFIED: Final[TypeAdapter[_Identified]] = TypeAdapter(_Identified)


class TelegramUpdates:
    """Long-polls `getUpdates` and reads each update into an Inbound item.

    An update of a kind it does not read, or a message without text, moves the batch's last
    update id and gives no item. A text message or a tap that lacks a field the adapter reads, an
    update without an integer id, and a reply that is not ok are a ChannelError: the batch is not
    journaled and the offset does not move past them. The error never carries the payload, which
    could hold a secret.
    """

    def __init__(self, api: BotApi) -> None:
        self._api = api

    async def fetch(self, offset: int, wait_s: float) -> Batch:
        body = await self._api.call(
            GET_UPDATES,
            {"offset": offset, "timeout": wait_s, "allowed_updates": list(ALLOWED_UPDATES)},
        )
        try:
            reply = _RESULT.validate_python(body)
        except ValidationError:
            raise ChannelError(NOT_ACCEPTED if _is_failed(body) else UNEXPECTED_REPLY) from None
        items: list[Inbound] = []
        last: int | None = None
        for raw in reply.result:
            update_id = _update_id(raw)
            last = update_id if last is None else max(last, update_id)
            if (item := _item(raw)) is not None:
                items.append(item)
        return Batch(last, tuple(items))


def _is_failed(body: object) -> bool:
    try:
        return _FAILED.validate_python(body).ok is False
    except ValidationError:
        return False


def _update_id(raw: object) -> int:
    try:
        return _IDENTIFIED.validate_python(raw).update_id
    except ValidationError:
        raise ChannelError(UNEXPECTED_REPLY) from None


def _item(raw: object) -> Inbound | None:
    try:
        update = _UPDATE.validate_python(raw)
    except ValidationError:
        raise ChannelError(UNEXPECTED_REPLY) from None
    if update.callback_query is not None:
        tap = update.callback_query
        ref = _ref(tap.message.chat.id, tap.message.message_id)
        return Inbound(update.update_id, InboundKind.TAP, tap.sender.id, ref, data=tap.data)
    if update.message is not None and update.message.text is not None:
        sent = update.message
        replied = sent.reply_to_message
        return Inbound(
            update.update_id,
            InboundKind.TEXT if replied is None else InboundKind.REPLY,
            sent.sender.id,
            _ref(sent.chat.id, sent.message_id),
            reply_to_ref=None if replied is None else _ref(sent.chat.id, replied.message_id),
            text=sent.text,
        )
    return None


def _ref(chat: int, message_id: int) -> str:
    """Return the reference the Channel gives a sent message, so inbound and outbound match."""
    return f"{chat}{REF_SEPARATOR}{message_id}"
