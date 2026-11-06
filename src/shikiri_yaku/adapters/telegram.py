"""The Channel over the Telegram Bot API: send and edit a message in the PO's chat."""

from __future__ import annotations

import contextlib
import html
import stat
from typing import TYPE_CHECKING, Final, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StrictInt, TypeAdapter, ValidationError

from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    Button,
    ChannelError,
    ChannelRefusedError,
    ChannelUnavailableError,
    CheckLine,
    join_blocks,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.ports import Clock, Copies, Message

TELEGRAM_CHANNEL: Final = "telegram"
TOKEN_MODE: Final = 0o600
OTHER_MEMBERS_NOTE: Final = "Other members of the group can read the messages; only owners can act"
SEND_MESSAGE: Final = "sendMessage"
EDIT_MESSAGE_TEXT: Final = "editMessageText"
# keep: the Bot API's parse mode for the markup a message carries
HTML_PARSE_MODE: Final = "HTML"
INPUT_PLACEHOLDER: Final = "Your answer"
DELETE_MESSAGE: Final = "deleteMessage"
GET_ME: Final = "getMe"
GET_CHAT: Final = "getChat"
GET_CHAT_MEMBER: Final = "getChatMember"
GET_CHAT_MEMBER_COUNT: Final = "getChatMemberCount"
GET_CHAT_ADMINISTRATORS: Final = "getChatAdministrators"
GET_WEBHOOK_INFO: Final = "getWebhookInfo"
CREATE_FORUM_TOPIC: Final = "createForumTopic"
CLOSE_FORUM_TOPIC: Final = "closeForumTopic"
REF_SEPARATOR: Final = "/"
SUPERGROUP: Final = "supergroup"
CREATOR: Final = "creator"
ADMINISTRATOR: Final = "administrator"
MEMBER: Final = "member"
RESTRICTED: Final = "restricted"
MEMBER_STATUSES: Final = frozenset({CREATOR, ADMINISTRATOR, MEMBER})

# keep: Telegram's error codes (Bot API, "Making requests"); the rest are transient
BAD_REQUEST: Final = 400
FORBIDDEN: Final = 403
TOO_MANY_REQUESTS: Final = 429
# keep: Telegram answered that a check does not hold; any other code may pass on a retry
DEFINITE_REFUSAL_CODES: Final = frozenset({BAD_REQUEST, FORBIDDEN})

# keep: the Channel's own reasons; platform text is never kept, since it could repeat a secret
TEXT_TOO_LONG: Final = "text_too_long"
BUTTON_DATA_TOO_LONG: Final = "button_data_too_long"
PLATFORM_REFUSED: Final = "platform_refused"
NOT_ACCEPTED: Final = "the Bot API did not accept the request"
UNEXPECTED_REPLY: Final = "the Bot API replied in a shape the Channel does not read"
BOT_NOT_IDENTIFIED: Final = "getMe did not identify a Telegram bot"
GROUP_NOT_READABLE: Final = "the Bot API could not read the configured group"
GROUP_NOT_FORUM: Final = "the configured group is not a forum supergroup"
BOT_TOPIC_RIGHTS: Final = "the bot must be an administrator with the manage-topics right"
OWNER_NOT_MEMBER: Final = "a configured owner is not a member of the group"
GROUP_HAS_OTHER_MEMBERS: Final = (
    "the group contains members other than its configured owners and bot"
)
GROUP_MEMBER_COUNT_UNAVAILABLE: Final = "the Bot API could not verify group membership"
# keep: the host only; a webhook URL can carry a path that is a secret
WEBHOOK_SET: Final = "a webhook is set, so getUpdates cannot read the PO's replies; host: {host}"
UNKNOWN_HOST: Final = "unknown"


class BotApi(Protocol):
    """One Bot API method call. It returns the decoded reply, whatever its `ok` says.

    It raises ChannelUnavailableError when no reply comes back at all.
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


class _User(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: StrictInt
    is_bot: bool


class _Accepted(BaseModel):
    """The fields of the bot's User object used by group validation."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _User


class _ChatInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str
    is_forum: bool | None = None


class _ChatReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _ChatInfo


class _ChatMember(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str
    user: _User
    can_manage_topics: bool | None = None
    is_member: bool | None = None


class _ChatMemberReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _ChatMember


class _ChatMemberCountReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: StrictInt


class _ChatAdministratorsReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: list[_ChatMember]


class _WebhookInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    url: str


class _WebhookReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _WebhookInfo


_PROBE_REPLY: Final[TypeAdapter[_Accepted | _Failed]] = TypeAdapter(_Accepted | _Failed)
_WEBHOOK_REPLY: Final[TypeAdapter[_WebhookReply | _Failed]] = TypeAdapter(_WebhookReply | _Failed)
_CHAT_REPLY: Final[TypeAdapter[_ChatReply | _Failed]] = TypeAdapter(_ChatReply | _Failed)
_CHAT_MEMBER_REPLY: Final[TypeAdapter[_ChatMemberReply | _Failed]] = TypeAdapter(
    _ChatMemberReply | _Failed
)
_CHAT_MEMBER_COUNT_REPLY: Final[TypeAdapter[_ChatMemberCountReply | _Failed]] = TypeAdapter(
    _ChatMemberCountReply | _Failed
)
_CHAT_ADMINISTRATORS_REPLY: Final[TypeAdapter[_ChatAdministratorsReply | _Failed]] = TypeAdapter(
    _ChatAdministratorsReply | _Failed
)


class _ForumTopic(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message_thread_id: StrictInt


class _CreatedTopic(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: _ForumTopic


class _ClosedTopic(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: Literal[True]
    result: Literal[True]


_CREATE_TOPIC_REPLY: Final[TypeAdapter[_CreatedTopic | _Failed]] = TypeAdapter(
    _CreatedTopic | _Failed
)
_CLOSE_TOPIC_REPLY: Final[TypeAdapter[_ClosedTopic | _Failed]] = TypeAdapter(_ClosedTopic | _Failed)


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
        """Delete the copy a secret was edited into, when the journal knows it.

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
    """Refuse a message whose texts carry a secret form; the form is the reason."""
    if (form := secret_form(message.texts())) is not None:
        raise ChannelRefusedError(form)


def _content(message: Message) -> dict[str, object]:
    """Refuse what Telegram would refuse before sending; then build the text and its markup.

    A link button has no callback data, so only the tap buttons are measured against its limit.
    The limit counts the characters the PO reads, not the markup that sets the heading in bold.
    """
    if len(message.shown()) > MESSAGE_TEXT_LIMIT:
        raise ChannelRefusedError(TEXT_TOO_LONG)
    for button in message.buttons:
        if not button.url and len(button.data.encode()) > CALLBACK_DATA_LIMIT:
            raise ChannelRefusedError(BUTTON_DATA_TOO_LONG)
    return {
        "text": _html_text(message),
        "parse_mode": HTML_PARSE_MODE,
        "reply_markup": _markup(message),
    }


def _markup(message: Message) -> dict[str, object]:
    """Return the reply box of a message that waits for text, else its keyboard.

    One button per row. An empty keyboard is what removes the buttons of a final state.
    Telegram takes one markup per message, so the reply box replaces the buttons.
    """
    if message.expects_reply:
        return {"force_reply": True, "input_field_placeholder": INPUT_PLACEHOLDER}
    keyboard = [[_keyboard_button(button)] for button in message.buttons]
    return {"inline_keyboard": keyboard}


def _html_text(message: Message) -> str:
    """Return the message in Telegram's HTML mode: the heading in bold, every text escaped.

    Telegram reads only `&`, `<` and `>` in text as markup, so those are escaped. The bold tags
    around the heading are the only markup the message adds.
    """
    heading = f"<b>{_escape(message.heading)}</b>" if message.heading else ""
    return join_blocks(heading, _escape(message.text))


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _keyboard_button(button: Button) -> dict[str, str]:
    if button.url:
        return {"text": button.label, "url": button.url}
    return {"text": button.label, "callback_data": button.data}


async def probe_bot(
    api: BotApi,
    group_id: int | None = None,
    owners: tuple[int, ...] = (),
    *,
    allow_other_members: bool = False,
    shared_group: bool = False,
) -> None:
    """Check bot authentication and the configured group's topic and membership requirements."""
    body = await api.call(GET_ME, {})
    reply = _validated(body, _PROBE_REPLY)
    if isinstance(reply, _Failed):
        raise ChannelUnavailableError(NOT_ACCEPTED)
    if not reply.result.is_bot:
        raise ChannelError(BOT_NOT_IDENTIFIED)
    await _probe_webhook(api)
    if group_id is not None:
        await _probe_group(
            api,
            group_id,
            owners,
            reply.result.id,
            allow_other_members=allow_other_members,
            shared_group=shared_group,
        )


class TelegramChannelCheck:
    """The Telegram adapter's implementation of the channel readiness check."""

    def __init__(
        self,
        api: BotApi,
        token_file: Path,
        group_id: int | None = None,
        owners: tuple[int, ...] = (),
        *,
        allow_other_members: bool = False,
        shared_group: bool = False,
    ) -> None:
        self._api = api
        self._token_file = token_file
        self._group_id = group_id
        self._owners = owners
        self._allow_other_members = allow_other_members
        self._shared_group = shared_group

    async def verify(self) -> None:
        await probe_bot(
            self._api,
            self._group_id,
            self._owners,
            allow_other_members=self._allow_other_members,
            shared_group=self._shared_group,
        )

    async def report(self) -> list[CheckLine]:
        lines = [CheckLine("token file", detail=_token_problem(self._token_file))]
        if self._allow_other_members:
            lines.append(CheckLine("group", status=OTHER_MEMBERS_NOTE))
        return lines


def _token_problem(token: Path) -> str | None:
    try:
        status = token.stat()
    except FileNotFoundError:
        return f"{token} does not exist"
    except OSError as error:
        return f"{token} cannot be read: {error}"
    if not stat.S_ISREG(status.st_mode):
        return f"{token} is not a regular file"
    mode = stat.S_IMODE(status.st_mode)
    return None if mode == TOKEN_MODE else f"{token} has mode {mode:04o}, must be {TOKEN_MODE:04o}"


async def _probe_webhook(api: BotApi) -> None:
    # keep: getUpdates is refused while a webhook is set, so the PO's replies would never be read
    reply = _validated(await api.call(GET_WEBHOOK_INFO, {}), _WEBHOOK_REPLY)
    if isinstance(reply, _Failed):
        raise ChannelUnavailableError(NOT_ACCEPTED)
    if reply.result.url:
        host = urlsplit(reply.result.url).hostname or UNKNOWN_HOST
        raise ChannelError(WEBHOOK_SET.format(host=host))


class TelegramForumTopics:
    """Create and close topics in the configured group through the Bot API."""

    def __init__(self, api: BotApi, group_id: int) -> None:
        self._api = api
        self._group_id = group_id

    async def create(self, name: str) -> int:
        body = await self._api.call(CREATE_FORUM_TOPIC, {"chat_id": self._group_id, "name": name})
        reply = _validated(body, _CREATE_TOPIC_REPLY)
        if isinstance(reply, _Failed):
            raise ChannelError(NOT_ACCEPTED)
        return reply.result.message_thread_id

    async def close(self, thread_id: int) -> None:
        body = await self._api.call(
            CLOSE_FORUM_TOPIC,
            {"chat_id": self._group_id, "message_thread_id": thread_id},
        )
        reply = _validated(body, _CLOSE_TOPIC_REPLY)
        if isinstance(reply, _Failed):
            raise ChannelError(NOT_ACCEPTED)


async def _probe_group(
    api: BotApi,
    group_id: int,
    owners: tuple[int, ...],
    bot_id: int,
    *,
    allow_other_members: bool,
    shared_group: bool,
) -> None:
    group = _validated(await api.call(GET_CHAT, {"chat_id": group_id}), _CHAT_REPLY)
    if isinstance(group, _Failed):
        raise _probe_refusal(group, GROUP_NOT_READABLE)
    if group.result.type != SUPERGROUP or group.result.is_forum is not True:
        raise ChannelError(GROUP_NOT_FORUM)

    bot = await _chat_member(api, group_id, bot_id, BOT_TOPIC_RIGHTS)
    if bot.user.id != bot_id or not bot.user.is_bot or not _has_topic_right(bot):
        raise ChannelError(BOT_TOPIC_RIGHTS)
    for owner_id in set(owners) - {bot_id}:
        owner = await _chat_member(api, group_id, owner_id, OWNER_NOT_MEMBER)
        if not _is_member(owner, owner_id):
            raise ChannelError(OWNER_NOT_MEMBER)

    if not allow_other_members:
        allowed = set(owners) | {bot_id}
        if shared_group:
            allowed |= await _bot_administrators(api, group_id, owners)
        await _probe_member_count(api, group_id, len(allowed))


async def _bot_administrators(api: BotApi, group_id: int, owners: tuple[int, ...]) -> set[int]:
    # keep: only administrator bots are listed, so a bot that is not an administrator is refused
    reply = _validated(
        await api.call(GET_CHAT_ADMINISTRATORS, {"chat_id": group_id, "return_bots": True}),
        _CHAT_ADMINISTRATORS_REPLY,
    )
    if isinstance(reply, _Failed):
        raise ChannelUnavailableError(GROUP_MEMBER_COUNT_UNAVAILABLE)
    bots: set[int] = set()
    for admin in reply.result:
        if admin.user.is_bot:
            bots.add(admin.user.id)
        elif admin.user.id not in owners:
            raise ChannelError(GROUP_HAS_OTHER_MEMBERS)
    return bots


async def _probe_member_count(api: BotApi, group_id: int, expected: int) -> None:
    # keep: every member of the group reads its messages, so by default it holds only the owners
    count = _validated(
        await api.call(GET_CHAT_MEMBER_COUNT, {"chat_id": group_id}), _CHAT_MEMBER_COUNT_REPLY
    )
    if isinstance(count, _Failed):
        raise ChannelUnavailableError(GROUP_MEMBER_COUNT_UNAVAILABLE)
    if count.result != expected:
        raise ChannelError(GROUP_HAS_OTHER_MEMBERS)


async def _chat_member(api: BotApi, group_id: int, user_id: int, failure: str) -> _ChatMember:
    reply = _validated(
        await api.call(GET_CHAT_MEMBER, {"chat_id": group_id, "user_id": user_id}),
        _CHAT_MEMBER_REPLY,
    )
    if isinstance(reply, _Failed):
        raise _probe_refusal(reply, failure)
    return reply.result


def _probe_refusal(reply: _Failed, failure: str) -> ChannelError:
    """Return the error of a refused check: definite for 400 and 403, otherwise unavailable."""
    if reply.error_code in DEFINITE_REFUSAL_CODES:
        return ChannelError(failure)
    return ChannelUnavailableError(failure)


def _has_topic_right(member: _ChatMember) -> bool:
    return member.status == CREATOR or (
        member.status == ADMINISTRATOR and member.can_manage_topics is True
    )


def _is_member(member: _ChatMember, user_id: int) -> bool:
    if member.user.id != user_id or member.user.is_bot:
        return False
    return member.status in MEMBER_STATUSES or (
        member.status == RESTRICTED and member.is_member is True
    )


def _validated[T](body: object, adapter: TypeAdapter[T]) -> T:
    try:
        return adapter.validate_python(body)
    except ValidationError as error:
        raise ChannelUnavailableError(UNEXPECTED_REPLY) from error
