"""The Telegram Channel against the Bot API replies in tests/fixtures/telegram/."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram import (
    BUTTON_DATA_TOO_LONG,
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    PHOTO_UNREADABLE,
    PLATFORM_REFUSED,
    TEXT_TOO_LONG,
    TelegramChannel,
    telegram_place,
)
from shikiri_yaku.domain.place import Place
from shikiri_yaku.ports import BLOCK_SEPARATOR, Button, ChannelError, ChannelRefusedError, Message
from tests.support.attachments import PNG

if TYPE_CHECKING:
    from collections.abc import Mapping

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
SENT: Final = "1001/42"
MESSAGE: Final = Message("S1 · Which way?", (Button("1", "a:1:1"),))
HEADING: Final = "S1 · Which way? · decision"


def _recorded(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return loaded


class _Api:
    """Answers each call with the next reply and keeps what was posted, and each upload."""

    def __init__(self, replies: list[object]) -> None:
        self._replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[tuple[str, dict[str, Any], str, str, bytes]] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self._replies.pop(0)

    async def upload(
        self, method: str, payload: Mapping[str, object], field: str, name: str, content: bytes
    ) -> object:
        self.uploads.append((method, dict(payload), field, name, content))
        return self._replies.pop(0)

    async def download(self, file_path: str) -> bytes:
        raise NotImplementedError(file_path)


class _Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _send(api: _Api, clock: _Clock, message: Message = MESSAGE) -> str:
    return asyncio.run(TelegramChannel(api, clock).send(Place("telegram", "1001"), message))


def test_a_photo_is_uploaded_with_its_text_as_the_caption(tmp_path: Path) -> None:
    photo = tmp_path / "sketch.png"
    photo.write_bytes(PNG)
    api = _Api([_recorded("send_ok.json")])

    assert _send(api, _Clock(), Message("the flow", photo=str(photo))) == SENT
    assert api.calls == []
    method, payload, field, name, content = api.uploads[0]
    assert (method, field, name, content) == ("sendPhoto", "photo", "sketch.png", PNG)
    assert payload["caption"] == "the flow"
    assert "text" not in payload


def test_a_photo_that_cannot_be_read_is_refused_before_any_call(tmp_path: Path) -> None:
    api = _Api([])

    with pytest.raises(ChannelRefusedError) as refused:
        _send(api, _Clock(), Message("", photo=str(tmp_path / "gone.png")))

    assert str(refused.value) == PHOTO_UNREADABLE
    assert (api.calls, api.uploads) == ([], [])


def test_send_posts_text_and_keyboard_and_returns_the_chat_and_message_ref() -> None:
    api = _Api([_recorded("send_ok.json")])

    assert _send(api, _Clock()) == SENT
    assert api.calls == [
        (
            "sendMessage",
            {
                "chat_id": "1001",
                "text": "S1 · Which way?",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "a:1:1"}]]},
            },
        )
    ]


def test_a_link_button_is_posted_as_a_url_beside_a_callback_button() -> None:
    api = _Api([_recorded("send_ok.json")])
    link = Message(
        "S1 · Pull request ready",
        (Button("Open PR", url="https://example.test/pull/7"), Button("Stop reminders", "k:1")),
    )

    _send(api, _Clock(), link)

    assert api.calls[0][1]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Open PR", "url": "https://example.test/pull/7"}],
            [{"text": "Stop reminders", "callback_data": "k:1"}],
        ]
    }


@pytest.mark.parametrize(
    ("data", "url"),
    [("", ""), ("", None), ("a:1:1", "https://example.test/pull/7")],
    ids=["neither", "neither, empty url", "both"],
)
def test_a_button_takes_exactly_one_of_data_or_url(data: str, url: str | None) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Button("1", data, url=url)


@pytest.mark.parametrize(
    ("message", "markup"),
    [
        (MESSAGE, {"inline_keyboard": [[{"text": "1", "callback_data": "a:1:1"}]]}),
        (Message("S1 · Which way?"), {"inline_keyboard": []}),
        (
            Message("S1 · Reply with your answer.", expects_reply=True),
            {"force_reply": True, "input_field_placeholder": "Your answer"},
        ),
    ],
    ids=["buttons", "no buttons", "waits for text"],
)
def test_a_message_is_sent_with_its_buttons_or_with_the_reply_box_when_it_waits_for_text(
    message: Message, markup: dict[str, object]
) -> None:
    api = _Api([_recorded("send_ok.json")])

    _send(api, _Clock(), message)

    assert api.calls[0][1]["reply_markup"] == markup


def test_a_message_takes_buttons_or_a_reply_box_but_not_both() -> None:
    with pytest.raises(ValueError, match="not both"):
        Message("S1 · Which way?", (Button("1", "a:1:1"),), expects_reply=True)


def test_send_posts_a_topic_thread_when_the_place_has_one() -> None:
    api = _Api([_recorded("send_ok.json")])

    asyncio.run(TelegramChannel(api, _Clock()).send(telegram_place(-1009, 73), MESSAGE))

    assert api.calls[0][1]["chat_id"] == "-1009"
    assert api.calls[0][1]["message_thread_id"] == 73


def test_send_replies_to_the_message_it_answers_even_if_that_message_is_gone() -> None:
    api = _Api([_recorded("send_ok.json")])
    answer = Message(
        "Open the Expert conversation.",
        (Button("Open topic", url="https://example.test/topic"),),
        reply_to="1001/42",
    )

    _send(api, _Clock(), answer)

    assert api.calls[0][1]["reply_parameters"] == {
        "message_id": 42,
        "allow_sending_without_reply": True,
    }


def test_edit_targets_the_ref_and_clears_buttons_with_an_empty_keyboard() -> None:
    api = _Api([_recorded("edit_ok.json")])

    channel = TelegramChannel(api, _Clock())
    asyncio.run(channel.edit(SENT, Message("S1 · Answered: option 1")))

    assert api.calls == [
        (
            "editMessageText",
            {
                "chat_id": "1001",
                "message_id": 42,
                "text": "S1 · Answered: option 1",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )
    ]


def test_a_question_is_sent_and_then_edited_with_its_text_escaped_and_its_heading_bold() -> None:
    question = Message("Use <b>bold</b> & keep?", (Button("1", "a:1:1"),), heading="Q & <A>")
    api = _Api([_recorded("send_ok.json"), _recorded("edit_ok.json")])
    channel = TelegramChannel(api, _Clock())

    ref = asyncio.run(channel.send(Place("telegram", "1001"), question))
    asyncio.run(channel.edit(ref, Message("Answered: <b> & more", heading="Q & <A>")))

    escaped_heading = "<b>Q &amp; &lt;A&gt;</b>"
    assert [(method, payload["text"], payload["parse_mode"]) for method, payload in api.calls] == [
        (
            "sendMessage",
            f"{escaped_heading}\n\nUse &lt;b&gt;bold&lt;/b&gt; &amp; keep?",
            "HTML",
        ),
        ("editMessageText", f"{escaped_heading}\n\nAnswered: &lt;b&gt; &amp; more", "HTML"),
    ]


def test_delete_posts_the_chat_and_message_of_the_reference() -> None:
    api = _Api([_recorded("delete_ok.json")])

    asyncio.run(TelegramChannel(api, _Clock()).delete(SENT))

    assert api.calls == [("deleteMessage", {"chat_id": "1001", "message_id": 42})]


def test_a_deletion_the_platform_refuses_is_a_refusal_and_not_transient() -> None:
    api = _Api([_recorded("message_too_long.json")])

    with pytest.raises(ChannelRefusedError):
        asyncio.run(TelegramChannel(api, _Clock()).delete(SENT))


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
        # keep: an ampersand is five characters of markup and one character the PO reads
        (Message("&" * MESSAGE_TEXT_LIMIT), SENT, 1),
        (
            Message(
                "x" * (MESSAGE_TEXT_LIMIT - len(HEADING) - len(BLOCK_SEPARATOR)), heading=HEADING
            ),
            SENT,
            1,
        ),
        (
            Message(
                "x" * (MESSAGE_TEXT_LIMIT - len(HEADING) - len(BLOCK_SEPARATOR) + 1),
                heading=HEADING,
            ),
            TEXT_TOO_LONG,
            0,
        ),
        (Message("S1", (Button("1", "a" * CALLBACK_DATA_LIMIT),)), SENT, 1),
        (
            Message(
                "S1", (Button("Open", url="https://example.test/" + "p" * CALLBACK_DATA_LIMIT * 2),)
            ),
            SENT,
            1,
        ),
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


@pytest.mark.parametrize(
    ("message", "replies", "reason"),
    [
        (Message("x" * (MESSAGE_TEXT_LIMIT + 1)), [], TEXT_TOO_LONG),
        (Message("S1", (Button("1", "a" * (CALLBACK_DATA_LIMIT + 1)),)), [], BUTTON_DATA_TOO_LONG),
        (MESSAGE, [_recorded("message_too_long.json")], PLATFORM_REFUSED),
    ],
)
def test_an_edit_the_platform_will_not_take_is_refused_with_its_reason(
    message: Message, replies: list[object], reason: str
) -> None:
    api = _Api(replies)

    with pytest.raises(ChannelRefusedError) as refused:
        asyncio.run(TelegramChannel(api, _Clock()).edit(SENT, message))

    assert str(refused.value) == reason
