"""The Telegram Channel against the Bot API replies in tests/fixtures/telegram/ (design §7, §12)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram import (
    BUTTON_DATA_TOO_LONG,
    PLATFORM_REFUSED,
    TEXT_TOO_LONG,
    TelegramChannel,
)
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    Button,
    ChannelError,
    ChannelRefusedError,
    Message,
)
from tests.support.secrets import SECRETS, secret_messages

if TYPE_CHECKING:
    from collections.abc import Mapping

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
SENT: Final = "1001/42"
MESSAGE: Final = Message("S1 · Which way?", (Button("1", "a:1:1"),))
TOKEN: Final = SECRETS["telegram_bot_token"]


def _recorded(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return loaded


class _Api:
    """Answers each call with the next reply and keeps what was posted."""

    def __init__(self, replies: list[object]) -> None:
        self._replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self._replies.pop(0)


class _Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _Copies:
    """The journal's side of an edit. It must hear of a deletion before the platform does."""

    def __init__(self, api: _Api, *, known: bool = True) -> None:
        self._api = api
        self._known = known
        self.refused: list[str] = []
        self.recorded: list[str] = []
        self.confirmed: list[str] = []

    def record_refused_edit(self, message_ref: str, reason: str, *, now: datetime) -> None:
        del message_ref, now
        self.refused.append(reason)

    def record_deletion(self, message_ref: str, reason: str, *, now: datetime) -> bool:
        del message_ref, now
        assert not self._api.calls, "the attempt is journaled before the platform is asked"
        self.recorded.append(reason)
        return self._known

    def confirm_deletion(self, message_ref: str) -> None:
        self.confirmed.append(message_ref)


def _send(api: _Api, clock: _Clock, message: Message = MESSAGE) -> str:
    return asyncio.run(
        TelegramChannel(api, clock, _Copies(api)).send(Destination("telegram", "1001"), message)
    )


def test_send_posts_text_and_keyboard_and_returns_the_chat_and_message_ref() -> None:
    api = _Api([_recorded("send_ok.json")])

    assert _send(api, _Clock()) == SENT
    assert api.calls == [
        (
            "sendMessage",
            {
                "chat_id": "1001",
                "text": "S1 · Which way?",
                "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "a:1:1"}]]},
            },
        )
    ]


def test_send_posts_a_topic_thread_when_the_destination_has_one() -> None:
    api = _Api([_recorded("send_ok.json")])

    asyncio.run(
        TelegramChannel(api, _Clock(), _Copies(api)).send(
            Destination("telegram", "-1009", 73), MESSAGE
        )
    )

    assert api.calls[0][1]["chat_id"] == "-1009"
    assert api.calls[0][1]["message_thread_id"] == 73


def test_edit_targets_the_ref_and_clears_buttons_with_an_empty_keyboard() -> None:
    api = _Api([_recorded("edit_ok.json")])

    channel = TelegramChannel(api, _Clock(), _Copies(api))
    asyncio.run(channel.edit(SENT, Message("S1 · Answered: option 1")))

    assert api.calls == [
        (
            "editMessageText",
            {
                "chat_id": "1001",
                "message_id": 42,
                "text": "S1 · Answered: option 1",
                "reply_markup": {"inline_keyboard": []},
            },
        )
    ]


def test_delete_posts_the_chat_and_message_of_the_reference() -> None:
    api = _Api([_recorded("delete_ok.json")])

    asyncio.run(TelegramChannel(api, _Clock(), _Copies(api)).delete(SENT))

    assert api.calls == [("deleteMessage", {"chat_id": "1001", "message_id": 42})]


def test_a_deletion_the_platform_refuses_is_a_refusal_and_not_transient() -> None:
    api = _Api([_recorded("message_too_long.json")])

    with pytest.raises(ChannelRefusedError):
        asyncio.run(TelegramChannel(api, _Clock(), _Copies(api)).delete(SENT))


def test_a_rate_limit_is_waited_out_once_and_the_send_is_repeated() -> None:
    clock = _Clock()
    api = _Api([_recorded("rate_limited.json"), _recorded("send_ok.json")])

    assert _send(api, clock) == SENT
    assert clock.slept == [3]
    assert len(api.calls) == 2


def test_a_second_rate_limit_is_transient_and_not_a_refusal() -> None:
    api = _Api([_recorded("rate_limited.json"), _recorded("rate_limited.json")])

    with pytest.raises(ChannelError):
        _send(api, _Clock())


def test_a_bad_request_is_refused_for_good_and_keeps_no_platform_text() -> None:
    api = _Api([_recorded("message_too_long.json")])

    with pytest.raises(ChannelRefusedError) as refused:
        _send(api, _Clock())

    assert str(refused.value) == PLATFORM_REFUSED


@pytest.mark.parametrize("error_code", [500, 502])
def test_a_server_failure_is_transient_so_the_sender_tries_again(error_code: int) -> None:
    api = _Api([{"ok": False, "error_code": error_code, "description": "Server error"}])

    with pytest.raises(ChannelError) as failed:
        _send(api, _Clock())

    assert not isinstance(failed.value, ChannelRefusedError)


@pytest.mark.parametrize("result", [{"message_id": 42}, True])
def test_a_send_reply_without_the_message_it_names_is_transient(result: object) -> None:
    api = _Api([{"ok": True, "result": result}])

    with pytest.raises(ChannelError):
        _send(api, _Clock())


def _sent_or_refused(message: Message) -> tuple[str, int]:
    """Return what the channel answered and how many calls it made to the platform."""
    api = _Api([_recorded("send_ok.json")])
    try:
        outcome = _send(api, _Clock(), message)
    except ChannelRefusedError as refusal:
        outcome = str(refusal)
    return outcome, len(api.calls)


@pytest.mark.parametrize(
    ("message", "outcome", "posts"),
    [
        (Message("x" * MESSAGE_TEXT_LIMIT), SENT, 1),
        (Message("x" * (MESSAGE_TEXT_LIMIT + 1)), TEXT_TOO_LONG, 0),
        (Message("S1", (Button("1", "a" * CALLBACK_DATA_LIMIT),)), SENT, 1),
        (Message("S1", (Button("1", "a" * (CALLBACK_DATA_LIMIT + 1)),)), BUTTON_DATA_TOO_LONG, 0),
        # keep: two bytes per letter, so half the limit in letters is exactly the limit in bytes
        (Message("S1", (Button("1", "é" * (CALLBACK_DATA_LIMIT // 2)),)), SENT, 1),
        (
            Message("S1", (Button("1", "é" * (CALLBACK_DATA_LIMIT // 2 + 1)),)),
            BUTTON_DATA_TOO_LONG,
            0,
        ),
    ],
)
def test_size_limits_are_refused_before_anything_is_posted(
    message: Message, outcome: str, posts: int
) -> None:
    assert _sent_or_refused(message) == (outcome, posts)


@pytest.mark.parametrize(("form", "message"), secret_messages())
def test_a_secret_in_any_place_of_a_message_is_refused_before_anything_is_posted(
    form: str, message: Message
) -> None:
    assert _sent_or_refused(message) == (form, 0)


@pytest.mark.parametrize(("form", "message"), secret_messages())
def test_an_edit_with_a_secret_journals_the_deletion_then_deletes_the_copy_and_is_refused(
    form: str, message: Message
) -> None:
    api = _Api([_recorded("delete_ok.json")])
    copies = _Copies(api)

    with pytest.raises(ChannelRefusedError) as refused:
        asyncio.run(TelegramChannel(api, _Clock(), copies).edit(SENT, message))

    assert str(refused.value) == form
    assert copies.recorded == [form]
    assert api.calls == [("deleteMessage", {"chat_id": "1001", "message_id": 42})]
    assert copies.confirmed == [SENT]


def test_an_edit_with_a_secret_deletes_nothing_when_the_journal_does_not_know_the_copy() -> None:
    api = _Api([])
    copies = _Copies(api, known=False)

    with pytest.raises(ChannelRefusedError):
        asyncio.run(TelegramChannel(api, _Clock(), copies).edit(SENT, Message(f"S1 {TOKEN}")))

    assert api.calls == []
    assert copies.confirmed == []


def test_a_failed_deletion_stays_journaled_and_the_refusal_stands() -> None:
    api = _Api([{"ok": False, "error_code": 400, "description": "Bad Request: can't delete"}])
    copies = _Copies(api)

    with pytest.raises(ChannelRefusedError):
        asyncio.run(TelegramChannel(api, _Clock(), copies).edit(SENT, Message(f"S1 {TOKEN}")))

    assert copies.recorded == ["telegram_bot_token"]
    assert copies.confirmed == []


@pytest.mark.parametrize(
    ("message", "replies", "reason"),
    [
        (Message("x" * (MESSAGE_TEXT_LIMIT + 1)), [], TEXT_TOO_LONG),
        (Message("S1", (Button("1", "a" * (CALLBACK_DATA_LIMIT + 1)),)), [], BUTTON_DATA_TOO_LONG),
        (MESSAGE, [_recorded("message_too_long.json")], PLATFORM_REFUSED),
    ],
)
def test_a_refused_edit_is_journaled_on_its_copy_and_no_copy_is_deleted(
    message: Message, replies: list[object], reason: str
) -> None:
    api = _Api(replies)
    copies = _Copies(api)

    with pytest.raises(ChannelRefusedError) as refused:
        asyncio.run(TelegramChannel(api, _Clock(), copies).edit(SENT, message))

    assert str(refused.value) == reason
    assert copies.refused == [reason]
    assert copies.recorded == []
