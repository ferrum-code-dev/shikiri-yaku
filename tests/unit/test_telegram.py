"""The Telegram Channel against recorded Bot API replies (design §7, §12)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram import BUTTON_DATA_TOO_LONG, TEXT_TOO_LONG, TelegramChannel
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    Button,
    ChannelError,
    ChannelRefusedError,
    Message,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
SENT: Final = "1001/42"
MESSAGE: Final = Message("S1 · Which way?", (Button("1", "a:1:1"),))


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


def _send(api: _Api, clock: _Clock, message: Message = MESSAGE) -> str:
    return asyncio.run(TelegramChannel(api, clock).send("1001", message))


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


def test_edit_targets_the_ref_and_clears_buttons_with_an_empty_keyboard() -> None:
    api = _Api([_recorded("edit_ok.json")])

    asyncio.run(TelegramChannel(api, _Clock()).edit(SENT, Message("S1 · Answered: option 1")))

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


def test_a_bad_request_is_refused_for_good_and_the_reply_says_why() -> None:
    reply = _recorded("message_too_long.json")
    api = _Api([reply])

    with pytest.raises(ChannelRefusedError) as refused:
        _send(api, _Clock())

    assert str(refused.value) == reply["description"]


@pytest.mark.parametrize("error_code", [500, 502])
def test_a_server_failure_is_transient_so_the_sender_tries_again(error_code: int) -> None:
    api = _Api([{"ok": False, "error_code": error_code, "description": "Server error"}])

    with pytest.raises(ChannelError) as failed:
        _send(api, _Clock())

    assert not isinstance(failed.value, ChannelRefusedError)


def test_a_reply_without_the_message_it_names_is_transient() -> None:
    api = _Api([{"ok": True, "result": {"message_id": 42}}])

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
