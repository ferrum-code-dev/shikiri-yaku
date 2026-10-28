"""The HTTP transport on a mock transport: replies, failures, the token and the log (design §7).

No socket is opened and no token file of the Operator is read, so the gate makes no network call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from shikiri_yaku.adapters.telegram_http import NO_REPLY, NO_TOKEN, HttpBotApi
from shikiri_yaku.ports import ChannelError
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from pathlib import Path

TOKEN: Final = "42:a-token-that-only-this-test-uses"  # noqa: S105 -- made up for this test
TIMEOUT_S: Final = 5
HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_BAD_GATEWAY: Final = 502
PAYLOAD: Final = {"chat_id": "1001", "text": "S1 · Which way?"}
SEND_URL: Final = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def _token_file(home: Path) -> Path:
    token = home / "telegram.token"
    token.write_text(TOKEN, encoding="utf-8")
    return token


def _reply(status: int, body: bytes, seen: list[httpx.Request]) -> httpx.MockTransport:
    """Answer every request with `status` and `body`, and keep the requests."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle)


def _failing(error: Exception, seen: list[httpx.Request]) -> httpx.MockTransport:
    """Fail every request with `error` before any reply, and keep the requests."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise error

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("status", "fixture"),
    [
        (HTTP_OK, "send_ok.json"),
        (HTTP_TOO_MANY_REQUESTS, "rate_limited.json"),
        (HTTP_BAD_REQUEST, "message_too_long.json"),
    ],
)
def test_a_documented_reply_comes_back_decoded_whatever_its_ok_says(
    tmp_path: Path, status: int, fixture: str
) -> None:
    body = (FIXTURES / fixture).read_bytes()
    transport = _reply(status, body, [])
    api = HttpBotApi(_token_file(tmp_path), TIMEOUT_S, transport=transport)

    assert asyncio.run(api.call("sendMessage", PAYLOAD)) == json.loads(body)


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("late")])
def test_no_reply_is_a_channel_error_that_names_neither_url_nor_token(
    tmp_path: Path, failure: Exception
) -> None:
    api = HttpBotApi(_token_file(tmp_path), TIMEOUT_S, transport=_failing(failure, []))

    with pytest.raises(ChannelError) as error:
        asyncio.run(api.call("sendMessage", PAYLOAD))
    assert str(error.value) == NO_REPLY


def test_a_body_that_is_not_json_is_a_channel_error(tmp_path: Path) -> None:
    transport = _reply(HTTP_BAD_GATEWAY, b"<html>Bad Gateway</html>", [])
    api = HttpBotApi(_token_file(tmp_path), TIMEOUT_S, transport=transport)

    with pytest.raises(ChannelError) as error:
        asyncio.run(api.call("sendMessage", PAYLOAD))
    assert str(error.value) == NO_REPLY


def test_the_token_is_in_the_request_url_and_nowhere_else(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "send_ok.json").read_bytes()
    api = HttpBotApi(_token_file(tmp_path), TIMEOUT_S, transport=_reply(HTTP_OK, body, seen))

    asyncio.run(api.call("sendMessage", PAYLOAD))

    [request] = seen
    assert str(request.url) == SEND_URL
    assert TOKEN.encode() not in request.content
    assert all(TOKEN not in value for value in request.headers.values())


def test_the_token_file_is_read_once_when_the_api_is_made(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "send_ok.json").read_bytes()
    token_file = _token_file(tmp_path)
    api = HttpBotApi(token_file, TIMEOUT_S, transport=_reply(HTTP_OK, body, seen))

    token_file.unlink()
    asyncio.run(api.call("sendMessage", PAYLOAD))

    [request] = seen
    assert str(request.url) == SEND_URL


def test_a_token_file_that_cannot_be_read_is_a_channel_error_and_no_request(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "send_ok.json").read_bytes()
    api = HttpBotApi(tmp_path / "missing.token", TIMEOUT_S, transport=_reply(HTTP_OK, body, seen))

    with pytest.raises(ChannelError) as error:
        asyncio.run(api.call("sendMessage", PAYLOAD))
    assert str(error.value) == NO_TOKEN
    assert seen == []


def test_the_token_is_never_logged_even_at_debug_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[httpx.Request] = []
    body = (FIXTURES / "send_ok.json").read_bytes()
    api = HttpBotApi(_token_file(tmp_path), TIMEOUT_S, transport=_reply(HTTP_OK, body, seen))
    caplog.set_level(logging.DEBUG)

    asyncio.run(api.call("sendMessage", PAYLOAD))

    assert seen
    assert TOKEN not in caplog.text
