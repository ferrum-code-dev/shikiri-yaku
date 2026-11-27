"""Fetches the files the PO attaches: `getFile` names the path, then the Bot API serves them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from shikiri_yaku.adapters.telegram import (
    BAD_REQUEST,
    NOT_ACCEPTED,
    PLATFORM_REFUSED,
    UNEXPECTED_REPLY,
)
from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.adapters.telegram import FileApi

GET_FILE: Final = "getFile"
# keep: the largest file the Bot API gives a bot; Telegram's limit, not a setting of ours
DOWNLOAD_LIMIT_BYTES: Final = 20 * 1024 * 1024


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Location(_Payload):
    file_path: str


class _Found(_Payload):
    ok: Literal[True]
    result: _Location


class _Refused(_Payload):
    ok: Literal[False]
    error_code: int


_REPLY: Final[TypeAdapter[_Found | _Refused]] = TypeAdapter(_Found | _Refused)


class TelegramFiles:
    """Gives the bytes of a file by its id. Telegram's 400 for a file it will not give is final.

    That covers a file over the download limit, so it is refused rather than tried again. Every
    other failure is a ChannelError, which the Expert tries again on its next tick.
    """

    def __init__(self, api: FileApi) -> None:
        self._api = api

    async def fetch(self, file_id: str) -> bytes:
        body = await self._api.call(GET_FILE, {"file_id": file_id})
        try:
            reply = _REPLY.validate_python(body)
        except ValidationError:
            raise ChannelError(UNEXPECTED_REPLY) from None
        if isinstance(reply, _Refused):
            if reply.error_code == BAD_REQUEST:
                raise ChannelRefusedError(PLATFORM_REFUSED)
            raise ChannelError(NOT_ACCEPTED)
        return await self._api.download(reply.result.file_path)
