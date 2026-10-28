"""The Bot API over HTTPS: the transport the Telegram Channel is given (design §7)."""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Final

from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

API_URL: Final = "https://api.telegram.org/bot{token}/{method}"
NO_REPLY: Final = "the Bot API could not be reached"


class HttpBotApi:
    """Posts each method as a JSON body and returns the decoded reply, whatever its `ok` says.

    The token is read from its file on each call, so a rotated token needs no restart and this
    object holds none. Only a missing reply is a ChannelError: an error reply is a reply, which the
    Channel reads. The error names neither the URL nor the token.
    """

    def __init__(self, token_file: Path, timeout_s: float, *, api_url: str = API_URL) -> None:
        self._token_file = token_file
        self._timeout_s = timeout_s
        self._api_url = api_url

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        try:
            return await asyncio.to_thread(self._post, method, payload)
        except OSError, ValueError, http.client.HTTPException:
            raise ChannelError(NO_REPLY) from None

    def _post(self, method: str, payload: Mapping[str, object]) -> object:
        token = self._token_file.read_text(encoding="utf-8").strip()
        url = self._api_url.format(token=token, method=method)
        request = urllib.request.Request(  # noqa: S310 -- an HTTPS API address, or a loopback test
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 -- an HTTPS API address, or a loopback test
                request, timeout=self._timeout_s
            ) as response:
                raw: bytes = response.read()
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read()
        reply: Any = json.loads(raw)
        return reply
