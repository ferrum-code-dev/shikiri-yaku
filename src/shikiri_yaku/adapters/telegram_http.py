"""The Bot API over HTTPS: the transport the Telegram Channel is given."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Final

import httpx

from shikiri_yaku.ports import ChannelRefusedError, ChannelUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

API_URL: Final = "https://api.telegram.org/bot{token}"
# keep: the Bot API serves the files a bot may download under this path, with the same token
FILE_URL: Final = "https://api.telegram.org/file/bot{token}"
HTTPX_LOGGER: Final = "httpx"
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR: Final = 500
NO_REPLY: Final = "the Bot API could not be reached"
NO_TOKEN: Final = "the token file cannot be read"  # noqa: S105 -- a message, not the token
FILE_REFUSED: Final = "the Bot API did not give the file"


class HttpBotApi:
    """Posts each method as a JSON body and returns the decoded reply, whatever its `ok` says.

    The token file is read once, here, and the token is kept only in the URLs of this object.
    A file that cannot be read is not an error here: each call then raises ChannelUnavailableError,
    so the service starts and `doctor` names the problem. An error reply is a reply, which the
    Channel reads. No error names the URL or the token.
    """

    def __init__(
        self,
        token_file: Path,
        timeout_s: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = _read_token(token_file)
        self._timeout_s = timeout_s
        self._transport = transport
        # keep: httpx logs every request's URL at INFO, and that URL carries the token
        logging.getLogger(HTTPX_LOGGER).setLevel(logging.WARNING)

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        url = self._endpoint(API_URL, method)
        return await self._decoded(lambda client: client.post(url, json=payload))

    async def upload(
        self,
        method: str,
        payload: Mapping[str, object],
        field: str,
        name: str,
        content: bytes,
    ) -> object:
        url = self._endpoint(API_URL, method)
        files = {field: (name, content)}
        return await self._decoded(
            lambda client: client.post(url, data=_form(payload), files=files)
        )

    async def download(self, file_path: str) -> bytes:
        url = self._endpoint(FILE_URL, file_path)
        response = await self._respond(lambda client: client.get(url))
        if response.is_success:
            return response.content
        if (
            response.status_code == HTTP_TOO_MANY_REQUESTS
            or response.status_code >= HTTP_SERVER_ERROR
        ):
            raise ChannelUnavailableError(NO_REPLY)
        raise ChannelRefusedError(FILE_REFUSED)

    @property
    def known_secrets(self) -> tuple[str, ...]:
        """Return the nonempty secret values loaded by this transport."""
        return (self._token,) if self._token else ()

    def _endpoint(self, template: str, tail: str) -> str:
        if self._token is None:
            raise ChannelUnavailableError(NO_TOKEN)
        return f"{template.format(token=self._token)}/{tail}"

    async def _decoded(
        self, send: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]
    ) -> object:
        response = await self._respond(send)
        try:
            return response.json()
        except ValueError:
            raise ChannelUnavailableError(NO_REPLY) from None

    async def _respond(
        self, send: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                return await send(client)
        except httpx.HTTPError:
            raise ChannelUnavailableError(NO_REPLY) from None


def _form(payload: Mapping[str, object]) -> dict[str, str]:
    # keep: a form carries strings, so a structured value goes as the JSON Telegram reads
    return {
        key: value if isinstance(value, str) else json.dumps(value)
        for key, value in payload.items()
    }


def _read_token(token_file: Path) -> str | None:
    """Return the token, or None when the token file cannot be read."""
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError, ValueError:
        return None
