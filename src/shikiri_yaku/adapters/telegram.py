"""The Channel over the Telegram Bot API: send and edit a message in the PO's chat (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    ChannelError,
    ChannelRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import Clock, Message

SEND_MESSAGE: Final = "sendMessage"
EDIT_MESSAGE_TEXT: Final = "editMessageText"
REF_SEPARATOR: Final = "/"

# keep: Telegram's error codes (Bot API, "Making requests"); the rest are transient
BAD_REQUEST: Final = 400
TOO_MANY_REQUESTS: Final = 429

# keep: reasons the Operator writes for a refusal it decides itself, before any call
TEXT_TOO_LONG: Final = "text_too_long"
BUTTON_DATA_TOO_LONG: Final = "button_data_too_long"
UNEXPECTED_REPLY: Final = "the Bot API replied in a shape the Channel does not read"


class BotApi(Protocol):
    """One Bot API method call. It returns the decoded reply, whatever its `ok` says.

    It raises ChannelError when no reply comes back at all.
    """

    async def call(self, method: str, payload: Mapping[str, object]) -> object: ...


class _Chat(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int


class _Sent(BaseModel):
    """The message object of a reply: the two fields a message reference is made of."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    message_id: int
    chat: _Chat


class _Ok(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _Sent


class _RetryAfter(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    retry_after: int


class _Failed(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[False]
    error_code: int
    description: str
    parameters: _RetryAfter | None = None


_REPLY: Final[TypeAdapter[_Ok | _Failed]] = TypeAdapter(_Ok | _Failed)


class TelegramChannel:
    """Sends and edits messages in one chat per destination.

    A message reference is `<chat id>/<message id>`, so `edit` can name the chat without being
    given the destination again. A rate limit is waited out once; a second one is a ChannelError,
    which the sender tries again on its next tick.
    """

    def __init__(self, api: BotApi, clock: Clock) -> None:
        self._api = api
        self._clock = clock

    async def send(self, destination: str, message: Message) -> str:
        sent = await self._call(SEND_MESSAGE, {"chat_id": destination, **_content(message)})
        return f"{sent.chat.id}{REF_SEPARATOR}{sent.message_id}"

    async def edit(self, message_ref: str, message: Message) -> None:
        chat, _, message_id = message_ref.partition(REF_SEPARATOR)
        payload: dict[str, object] = {
            "chat_id": chat,
            "message_id": int(message_id),
            **_content(message),
        }
        await self._call(EDIT_MESSAGE_TEXT, payload)

    async def _call(self, method: str, payload: Mapping[str, object]) -> _Sent:
        reply = await self._post(method, payload)
        if (
            isinstance(reply, _Failed)
            and reply.error_code == TOO_MANY_REQUESTS
            and reply.parameters is not None
        ):
            await self._clock.sleep(reply.parameters.retry_after)
            reply = await self._post(method, payload)
        if isinstance(reply, _Ok):
            return reply.result
        if reply.error_code == BAD_REQUEST:
            raise ChannelRefusedError(reply.description)
        raise ChannelError(reply.description)

    async def _post(self, method: str, payload: Mapping[str, object]) -> _Ok | _Failed:
        body = await self._api.call(method, payload)
        try:
            return _REPLY.validate_python(body)
        except ValidationError as error:
            raise ChannelError(UNEXPECTED_REPLY) from error


def _content(message: Message) -> dict[str, object]:
    """Refuse what Telegram would refuse before sending; then build the text and keyboard.

    One button per row. An empty keyboard is what removes the buttons of a final state (design §7).
    """
    if len(message.text) > MESSAGE_TEXT_LIMIT:
        raise ChannelRefusedError(TEXT_TOO_LONG)
    for button in message.buttons:
        if len(button.data.encode()) > CALLBACK_DATA_LIMIT:
            raise ChannelRefusedError(BUTTON_DATA_TOO_LONG)
    keyboard = [
        [{"text": button.label, "callback_data": button.data}] for button in message.buttons
    ]
    return {"text": message.text, "reply_markup": {"inline_keyboard": keyboard}}
