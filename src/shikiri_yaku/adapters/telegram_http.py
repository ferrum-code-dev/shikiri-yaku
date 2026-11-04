"""The Bot API over HTTPS: the transport the Telegram Channel is given."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import httpx

from shikiri_yaku.ports import ChannelUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

API_URL: Final = "https://api.telegram.org/bot{token}"
HTTPX_LOGGER: Final = "httpx"
NO_REPLY: Final = "the Bot API could not be reached"
NO_TOKEN: Final = "the token file cannot be read"  # noqa: S105 -- a message, not the token


class HttpBotApi:
    """Posts each method as a JSON body and returns the decoded reply, whatever its `ok` says.

    The token file is read once, here, and the token is kept only in the base URL of this object.
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
        self._base_url = _base_url(token_file)
        self._timeout_s = timeout_s
        self._transport = transport
        # keep: httpx logs every request's URL at INFO, and that URL carries the token
        logging.getLogger(HTTPX_LOGGER).setLevel(logging.WARNING)

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if self._base_url is None:
            raise ChannelUnavailableError(NO_TOKEN)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(f"{self._base_url}/{method}", json=payload)
            reply: object = response.json()
        except httpx.HTTPError, ValueError:
            raise ChannelUnavailableError(NO_REPLY) from None
        return reply


def _base_url(token_file: Path) -> str | None:
    """Return the API URL with the token in it, or None when the token file cannot be read."""
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError, ValueError:
        return None
    return API_URL.format(token=token)
