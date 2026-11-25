"""Fetching an attached file: getFile names its path, and the Bot API then serves the bytes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram_files import TelegramFiles
from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from collections.abc import Mapping

CONTENT: Final = b"file bytes"


class _Api:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self.reply

    async def upload(
        self, method: str, payload: Mapping[str, object], field: str, name: str, content: bytes
    ) -> object:
        raise NotImplementedError(method, payload, field, name, content)

    async def download(self, file_path: str) -> bytes:
        self.downloads.append(file_path)
        return CONTENT


def test_a_file_is_fetched_by_the_path_getfile_names() -> None:
    api = _Api({"ok": True, "result": {"file_id": "file-1", "file_path": "documents/a.pdf"}})

    assert asyncio.run(TelegramFiles(api).fetch("file-1")) == CONTENT
    assert api.calls == [("getFile", {"file_id": "file-1"})]
    assert api.downloads == ["documents/a.pdf"]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        pytest.param(
            {"ok": False, "error_code": 400, "description": "file is too big"},
            ChannelRefusedError,
            id="refused-for-good",
        ),
        pytest.param(
            {"ok": False, "error_code": 500, "description": "try later"},
            ChannelError,
            id="failed-for-now",
        ),
        pytest.param({"ok": True, "result": {"unexpected": 1}}, ChannelError, id="unreadable"),
    ],
)
def test_a_getfile_reply_that_gives_no_path_is_refused_or_retried_by_its_kind(
    reply: object, error: type[Exception]
) -> None:
    api = _Api(reply)

    with pytest.raises(error):
        asyncio.run(TelegramFiles(api).fetch("file-1"))
    assert api.downloads == []
