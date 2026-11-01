"""The redaction of recorded Bot API replies: the token and the ids are replaced everywhere (E3)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.telegram_redact import (
    ID_PLACEHOLDER,
    TOKEN_PLACEHOLDER,
    RecordingLeakError,
    Secrets,
    holds_secret,
    leaks,
    redact,
    redact_recording,
)
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from pathlib import Path

TOKEN: Final = "123456789:" + "A" * 35
OTHER_TOKEN: Final = "987654321:" + "B" * 35
PO_CHAT: Final = 1001
BOT_ID: Final = 123456789
STRANGER: Final = 222333444
SECRETS: Final = Secrets(TOKEN, frozenset({PO_CHAT, 5550001, BOT_ID}))
PLAIN_REPLY: Final = {"update_id": 700, "message_id": 42, "date": 1793174400, "text": "Use it"}
_REPLACED: list[tuple[object, object]] = [
    ({"result": {"chat": {"id": PO_CHAT}}}, {"result": {"chat": {"id": ID_PLACEHOLDER}}}),
    ([PO_CHAT, 42], [ID_PLACEHOLDER, 42]),
    ({"from": {"id": 5550001, "is_bot": True}}, {"from": {"id": ID_PLACEHOLDER, "is_bot": True}}),
    ({"text": f"sent with {TOKEN}"}, {"text": f"sent with {TOKEN_PLACEHOLDER}"}),
    ({"text": f"Pavel {PO_CHAT} said"}, {"text": f"Pavel {ID_PLACEHOLDER} said"}),
    ({"text": f"{BOT_ID}:tail"}, {"text": f"{ID_PLACEHOLDER}:tail"}),
    ({TOKEN: "key"}, {TOKEN_PLACEHOLDER: "key"}),
    (
        {"url": f"https://api.telegram.org/bot{TOKEN}/getMe"},
        {"url": f"https://api.telegram.org/bot{TOKEN_PLACEHOLDER}/getMe"},
    ),
]
STRANGER_UPDATES: Final = {
    "ok": True,
    "result": [
        {
            "update_id": 701,
            "message": {
                "message_id": 43,
                "from": {"id": STRANGER, "is_bot": False},
                "chat": {"id": STRANGER},
                "text": f"hello from {STRANGER}",
                "reply_to_message": {"message_id": 42, "from": {"id": 333444555}},
            },
        }
    ],
}
REDACTED_STRANGER: Final = {
    "ok": True,
    "result": [
        {
            "update_id": 701,
            "message": {
                "message_id": 43,
                "from": {"id": ID_PLACEHOLDER, "is_bot": False},
                "chat": {"id": ID_PLACEHOLDER},
                "text": f"hello from {ID_PLACEHOLDER}",
                "reply_to_message": {"message_id": 42, "from": {"id": ID_PLACEHOLDER}},
            },
        }
    ],
}
_HOLDS: list[tuple[object, bool]] = [
    (PLAIN_REPLY, False),
    ({"result": {"chat": {"id": ID_PLACEHOLDER}, "text": f"{TOKEN_PLACEHOLDER} 0"}}, False),
    ({"result": {"chat": {"id": PO_CHAT}}}, True),
    ({"result": {"from": {"id": STRANGER}}}, True),
    ({"text": f"bot {OTHER_TOKEN}"}, True),
    ({"text": f"token {TOKEN}"}, True),
]


@pytest.mark.parametrize(("payload", "expected"), _REPLACED)
def test_the_token_and_the_ids_are_replaced_wherever_they_sit(
    payload: object, expected: object
) -> None:
    assert redact(payload, SECRETS) == expected


@pytest.mark.parametrize("payload", [PLAIN_REPLY, [], "no secret here"])
def test_a_payload_without_them_comes_back_unchanged(payload: object) -> None:
    assert redact(payload, SECRETS) == payload


def test_an_extra_sender_is_replaced_everywhere_the_replies_name_it() -> None:
    redacted = redact_recording({"get_updates_ok": STRANGER_UPDATES}, SECRETS)

    assert redacted == {"get_updates_ok": REDACTED_STRANGER}


def test_a_reply_that_still_holds_another_bot_token_is_refused() -> None:
    with pytest.raises(RecordingLeakError):
        redact_recording({"send_ok": {"text": f"from {OTHER_TOKEN}"}}, SECRETS)


@pytest.mark.parametrize(("payload", "unsafe"), _HOLDS)
def test_a_recording_that_still_holds_a_token_or_an_id_is_unsafe(
    payload: object, *, unsafe: bool
) -> None:
    assert holds_secret(payload, SECRETS) is unsafe


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.name)
def test_no_documented_reply_keeps_the_token_or_an_id(path: Path) -> None:
    documented = {path.name: json.loads(path.read_text(encoding="utf-8"))}
    redacted = redact_recording(documented, SECRETS)

    assert not holds_secret(redacted[path.name], SECRETS)


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        (f"token {TOKEN}", True),
        (f"the chat {PO_CHAT}", True),
        (f"the bot {BOT_ID}", True),
        (f"{TOKEN_PLACEHOLDER} chat {ID_PLACEHOLDER}", False),
        ("a message of 42 bytes", False),
    ],
)
def test_a_text_that_still_holds_the_token_or_an_id_is_a_leak(text: str, *, leaked: bool) -> None:
    assert leaks(text, SECRETS) is leaked
