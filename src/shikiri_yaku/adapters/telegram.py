"""The Channel over the Telegram Bot API: send and edit a message in the PO's chat (design §7)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    ChannelError,
    ChannelRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import Clock, Copies, Message

SEND_MESSAGE: Final = "sendMessage"
EDIT_MESSAGE_TEXT: Final = "editMessageText"
DELETE_MESSAGE: Final = "deleteMessage"
GET_ME: Final = "getMe"
REF_SEPARATOR: Final = "/"

# keep: Telegram's error codes (Bot API, "Making requests"); the rest are transient
BAD_REQUEST: Final = 400
TOO_MANY_REQUESTS: Final = 429

# keep: the Channel's own reasons; platform text is never kept, since it could repeat a secret
TEXT_TOO_LONG: Final = "text_too_long"
BUTTON_DATA_TOO_LONG: Final = "button_data_too_long"
PLATFORM_REFUSED: Final = "platform_refused"
NOT_ACCEPTED: Final = "the Bot API did not accept the request"
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
    """A successful reply: a sent message, or `true` for a method that returns nothing else."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _Sent | Literal[True]


class _RetryAfter(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    retry_after: int


class _Failed(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[False]
    error_code: int
    parameters: _RetryAfter | None = None


_REPLY: Final[TypeAdapter[_Ok | _Failed]] = TypeAdapter(_Ok | _Failed)


class _Accepted(BaseModel):
    """A successful reply to `getMe`, whose User object the probe does not read."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]


_PROBE_REPLY: Final[TypeAdapter[_Accepted | _Failed]] = TypeAdapter(_Accepted | _Failed)


class TelegramChannel:
    """Sends, edits and deletes messages in one chat per destination.

    A message reference is `<chat id>/<message id>`, so `edit` can name the chat without being
    given the destination again. A rate limit is waited out once; a second one is a ChannelError,
    which the sender tries again on its next tick. A secret is refused before any call. A refused
    edit is journaled on its copy, and a secret edit also deletes that copy first.
    """

    def __init__(self, api: BotApi, clock: Clock, copies: Copies) -> None:
        self._api = api
        self._clock = clock
        self._copies = copies

    async def send(self, destination: Destination, message: Message) -> str:
        _refuse_secret(message)
        payload: dict[str, object] = {"chat_id": destination.chat, **_content(message)}
        if destination.thread is not None:
            payload["message_thread_id"] = destination.thread
        reply = await self._request(SEND_MESSAGE, payload)
        if not isinstance(reply.result, _Sent):
            raise ChannelError(UNEXPECTED_REPLY)
        return f"{reply.result.chat.id}{REF_SEPARATOR}{reply.result.message_id}"

    async def edit(self, message_ref: str, message: Message) -> None:
        if (form := secret_form(message.texts())) is not None:
            await self._delete_copy(message_ref, form)
            raise ChannelRefusedError(form)
        try:
            await self._request(EDIT_MESSAGE_TEXT, {**_target(message_ref), **_content(message)})
        except ChannelRefusedError as refusal:
            self._copies.record_refused_edit(message_ref, str(refusal), now=self._clock.now())
            raise

    async def delete(self, message_ref: str) -> None:
        await self._request(DELETE_MESSAGE, _target(message_ref))

    async def _delete_copy(self, message_ref: str, reason: str) -> None:
        """Delete the copy a secret was edited into, when the journal knows it (design §7).

        The attempt is journaled, and the PO's notice queued, before the platform is asked. A failed
        deletion stays journaled and is not retried; the refusal stands either way.
        """
        if not self._copies.record_deletion(message_ref, reason, now=self._clock.now()):
            return
        with contextlib.suppress(ChannelError, ChannelRefusedError):
            await self._request(DELETE_MESSAGE, _target(message_ref))
            self._copies.confirm_deletion(message_ref)

    async def _request(self, method: str, payload: Mapping[str, object]) -> _Ok:
        reply = await self._post(method, payload)
        if (
            isinstance(reply, _Failed)
            and reply.error_code == TOO_MANY_REQUESTS
            and reply.parameters is not None
        ):
            await self._clock.sleep(reply.parameters.retry_after)
            reply = await self._post(method, payload)
        if isinstance(reply, _Ok):
            return reply
        if reply.error_code == BAD_REQUEST:
            raise ChannelRefusedError(PLATFORM_REFUSED)
        raise ChannelError(NOT_ACCEPTED)

    async def _post(self, method: str, payload: Mapping[str, object]) -> _Ok | _Failed:
        body = await self._api.call(method, payload)
        try:
            return _REPLY.validate_python(body)
        except ValidationError as error:
            raise ChannelError(UNEXPECTED_REPLY) from error


def _target(message_ref: str) -> dict[str, object]:
    """Return the chat and the message a reference names, as the Bot API takes them."""
    chat, _, message_id = message_ref.partition(REF_SEPARATOR)
    return {"chat_id": chat, "message_id": int(message_id)}


def _refuse_secret(message: Message) -> None:
    """Refuse a message whose texts carry a secret form; the form is the reason (design §7)."""
    if (form := secret_form(message.texts())) is not None:
        raise ChannelRefusedError(form)


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


async def probe_bot(api: BotApi) -> None:
    """Ask the Bot API who the bot is, for `doctor`; raises ChannelError if it refuses the call."""
    body = await api.call(GET_ME, {})
    try:
        reply = _PROBE_REPLY.validate_python(body)
    except ValidationError as error:
        raise ChannelError(UNEXPECTED_REPLY) from error
    if isinstance(reply, _Failed):
        raise ChannelError(NOT_ACCEPTED)
