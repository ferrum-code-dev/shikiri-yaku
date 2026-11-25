"""Receive conversation-local files before a turn is eligible to run."""

from __future__ import annotations

import csv
import io
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from PIL import Image, ImageSequence
from pydantic import JsonValue, TypeAdapter, ValidationError

from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from shikiri_yaku.app.expert import ConversationLog
    from shikiri_yaku.domain.inbound import Attachment

_TEXT: Final = frozenset({".txt", ".md", ".csv", ".json"})
_IMAGES: Final = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
_JSON: TypeAdapter[object] = TypeAdapter(JsonValue)
_PRIVATE_DIRECTORY: Final = 0o700
_PENDING: Final = "pending"
_READY: Final = "ready"
_NUL: Final = "\0"
_JSON_SUFFIX: Final = ".json"
_CSV_SUFFIX: Final = ".csv"


class FileDownloader(Protocol):
    """The channel streams one file into a new private temporary file."""

    async def download(self, file_id: str, target: Path, max_bytes: int) -> int: ...


class ExpertFiles:
    """Reserve, download, validate and publish a file for one conversation."""

    def __init__(
        self,
        log: ConversationLog,
        downloader: FileDownloader,
        directory: Path,
        *,
        max_file: int,
        max_total: int,
    ) -> None:
        self._log = log
        self._downloader = downloader
        self._directory = directory
        self._max_file = max_file
        self._max_total = max_total

    async def receive(self, conversation_id: int, update_id: int, file: Attachment) -> bool:
        """Return whether the file was safely retained; never pass it to a provider."""
        suffix = Path(file.name).suffix.lower()
        if (suffix not in _TEXT and suffix not in _IMAGES) or (
            file.size is not None and file.size > self._max_file
        ):
            return False
        row = self._log.reserve_attachment(
            conversation_id, update_id, self._max_file, self._max_total
        )
        if row is None:
            return False
        name, state, limit = row
        if state != _PENDING:
            return state == _READY
        directory = conversation_directory(self._directory, conversation_id)
        partial = directory / (name + ".part")
        target = directory / name
        try:
            directory.mkdir(mode=_PRIVATE_DIRECTORY, parents=True, exist_ok=True)
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            size = await self._downloader.download(file.file_id, partial, limit)
            if not _valid(partial, suffix):
                self._log.finish_attachment(update_id, "failed", 0)
                return False
            partial.replace(target)
            self._log.finish_attachment(update_id, _READY, size)
        except ChannelError, OSError:
            self._log.finish_attachment(update_id, "failed", 0)
            return False
        else:
            return True
        finally:
            with suppress(OSError):
                partial.unlink(missing_ok=True)


def _valid(path: Path, suffix: str) -> bool:
    if suffix in _IMAGES:
        return _valid_image(path, _IMAGES[suffix])
    try:
        text = path.read_bytes().decode("utf-8")
        if _NUL in text:
            return False
        if suffix == _JSON_SUFFIX:
            _JSON.validate_json(text)
        elif suffix == _CSV_SUFFIX:
            list(csv.reader(io.StringIO(text), strict=True))
    except OSError, UnicodeError, ValidationError, csv.Error:
        return False
    else:
        return True


def _valid_image(path: Path, image_format: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=[image_format]) as image:
                image.verify()
            with Image.open(path, formats=[image_format]) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False
    else:
        return True
