"""The Bot API over HTTPS: the transport the Telegram Channel is given."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from shikiri_yaku.ports import ChannelUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

API_URL: Final = "https://api.telegram.org/bot{token}"
HTTPX_LOGGER: Final = "httpx"
NO_REPLY: Final = "the Bot API could not be reached"
NO_TOKEN: Final = "the token file cannot be read"  # noqa: S105 -- a message, not the token
FILE_UNAVAILABLE: Final = "the file could not be downloaded"
FILE_TOO_LARGE: Final = "the file exceeds the download limit"
PRIVATE_FILE_MODE: Final = 0o600
INVALID_PATH_PARTS: Final = frozenset({".", ".."})
BACKSLASH: Final = "\\"


def _check_size(size: int, limit: int) -> None:
    if size > limit:
        raise ChannelUnavailableError(FILE_TOO_LARGE)


def _remove_partial(target: Path) -> None:
    target.unlink(missing_ok=True)


class _TelegramFile(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    file_path: str
    file_size: int | None = None


class _FileReply(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    ok: bool
    result: _TelegramFile | None = None


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

    async def download(self, file_id: str, target: Path, max_bytes: int) -> int:
        """Stream a Telegram file into a new private file, enforcing its actual size."""
        if self._base_url is None:
            raise ChannelUnavailableError(NO_TOKEN)
        try:
            reply = _FileReply.model_validate(await self.call("getFile", {"file_id": file_id}))
            if not reply.ok or reply.result is None:
                raise ChannelUnavailableError(FILE_UNAVAILABLE)
            remote = reply.result
            if remote.file_size is not None and (
                remote.file_size < 0 or remote.file_size > max_bytes
            ):
                raise ChannelUnavailableError(FILE_TOO_LARGE)
            parts = remote.file_path.split("/")
            if any(not part or part in INVALID_PATH_PARTS or BACKSLASH in part for part in parts):
                raise ChannelUnavailableError(FILE_UNAVAILABLE)
            url = (
                self._base_url.replace("/bot", "/file/bot", 1)
                + "/"
                + "/".join(quote(part, safe="") for part in parts)
            )
            size = 0
            created = False
            try:
                async with (
                    httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client,
                    client.stream("GET", url) as response,
                ):
                    response.raise_for_status()
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
                    created = True
                    with os.fdopen(fd, "wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            _check_size(size, max_bytes)
                            output.write(chunk)
            except BaseException:
                if created:
                    _remove_partial(target)
                raise
            else:
                return size
        except ValidationError, httpx.HTTPError, OSError, ValueError:
            raise ChannelUnavailableError(FILE_UNAVAILABLE) from None

    @property
    def known_secrets(self) -> tuple[str, ...]:
        """Return the nonempty secret values loaded by this transport."""
        token = "" if self._base_url is None else self._base_url.removeprefix(API_URL.split("{")[0])
        return (token,) if token else ()


def _base_url(token_file: Path) -> str | None:
    """Return the API URL with the token in it, or None when the token file cannot be read."""
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError, ValueError:
        return None
    return API_URL.format(token=token)
