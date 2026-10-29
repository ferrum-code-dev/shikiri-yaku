"""The PO's side of the Telegram chat: `getUpdates` over the Bot API (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

from shikiri_yaku.adapters.telegram import REF_SEPARATOR, UNEXPECTED_REPLY
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.ports import ChannelProtocolError

if TYPE_CHECKING:
    from shikiri_yaku.adapters.telegram import BotApi

GET_UPDATES: Final = "getUpdates"
ALLOWED_UPDATES: Final = ("message", "callback_query")


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Identity(_Payload):
    id: int


class _Replied(_Payload):
    message_id: int


class _Message(_Payload):
    message_id: int
    sender: Annotated[_Identity, Field(alias="from")]
    chat: _Identity
    text: str | None = None
    message_thread_id: StrictInt | None = None
    reply_to_message: _Replied | None = None


class _Tapped(_Payload):
    message_id: int
    chat: _Identity


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


_RESULT: Final[TypeAdapter[_Result]] = TypeAdapter(_Result)


class TelegramUpdates:
    """Long-polls `getUpdates` and reads each update into an Inbound item.

    Unsupported kinds and messages without text give no item but retain their update id. So does
    an item from a chat that is neither a private chat nor the configured group.
    Invalid supported updates fail the whole batch without exposing their payload in errors.
    """

    def __init__(self, api: BotApi, *, group_id: int | None) -> None:
        self._api = api
        self._group_id = group_id

    async def fetch(self, offset: int, wait_s: float) -> Batch:
        body = await self._api.call(
            GET_UPDATES,
            {"offset": offset, "timeout": wait_s, "allowed_updates": list(ALLOWED_UPDATES)},
        )
        try:
            reply = _RESULT.validate_python(body)
        except ValidationError:
            raise ChannelProtocolError(UNEXPECTED_REPLY) from None
        items: list[Inbound] = []
        last: int | None = None
        for update in reply.result:
            last = update.update_id if last is None else max(last, update.update_id)
            if (item := _item(update)) is not None and item.chat in (item.sender, self._group_id):
                items.append(item)
        return Batch(last, tuple(items))


def _item(update: _Update) -> Inbound | None:
    if update.callback_query is not None:
        tap = update.callback_query
        ref = _ref(tap.message.chat.id, tap.message.message_id)
        return Inbound(
            update.update_id,
            InboundKind.TAP,
            tap.sender.id,
            tap.message.chat.id,
            ref,
            data=tap.data,
        )
    if update.message is not None and update.message.text is not None:
        sent = update.message
        replied = sent.reply_to_message
        return Inbound(
            update.update_id,
            InboundKind.TEXT if replied is None else InboundKind.REPLY,
            sent.sender.id,
            sent.chat.id,
            _ref(sent.chat.id, sent.message_id),
            reply_to_ref=None if replied is None else _ref(sent.chat.id, replied.message_id),
            text=sent.text,
            thread=sent.message_thread_id,
        )
    return None


def _ref(chat: int, message_id: int) -> str:
    return f"{chat}{REF_SEPARATOR}{message_id}"
