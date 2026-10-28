"""The HTTP transport against a loopback server: an error reply is a reply, and no token leaks.

The server listens on 127.0.0.1 only, so the real Bot API address is never reached (design §7).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.telegram_http import HttpBotApi
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

TOKEN: Final = "42:a-token-that-only-this-test-uses"  # noqa: S105 -- made up for this test
TIMEOUT_S: Final = 5
HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
PAYLOAD: Final = {"chat_id": "1001", "text": "S1 · Which way?"}
BAD_REQUEST_REPLY: Final = (
    b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'
)


@contextlib.contextmanager
def _server(status: int, body: bytes) -> Generator[str]:
    """Answer every POST on loopback with `status` and `body`, and yield the URL template."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _closed_url() -> str:
    """Return a URL template whose server is already shut down, so its port refuses connections."""
    with _server(HTTP_OK, b"{}") as url:
        return url


def _token_file(home: Path) -> Path:
    token = home / "telegram.token"
    token.write_text(TOKEN, encoding="utf-8")
    return token


def test_an_error_reply_comes_back_as_the_reply_the_bot_api_sent(tmp_path: Path) -> None:
    token = _token_file(tmp_path)

    with _server(HTTP_BAD_REQUEST, BAD_REQUEST_REPLY) as url:
        transport = HttpBotApi(token, TIMEOUT_S, api_url=url)
        reply = asyncio.run(transport.call("sendMessage", PAYLOAD))

    assert reply == json.loads(BAD_REQUEST_REPLY)


def test_a_missing_reply_is_a_channel_error_that_does_not_name_the_token(tmp_path: Path) -> None:
    token = _token_file(tmp_path)
    transport = HttpBotApi(token, TIMEOUT_S, api_url=_closed_url())

    with pytest.raises(ChannelError) as error:
        asyncio.run(transport.call("sendMessage", PAYLOAD))
    assert TOKEN not in str(error.value)
