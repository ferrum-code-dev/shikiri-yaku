"""Fakes for the files an owner attaches and the pictures an Expert asks for."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import ChannelRefusedError, ChannelUnavailableError, SketchError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

PNG: Final = b"\x89PNG\r\n\x1a\n" + b"fake picture"
REFUSAL: Final = "fake refusal"
OUTAGE: Final = "fake outage"


class FakeDownloads:
    """Serves the bytes of each file id. A refused id is refused; an unavailable one fails once."""

    def __init__(
        self,
        files: Mapping[str, bytes] | None = None,
        *,
        refused: frozenset[str] = frozenset(),
        unavailable: frozenset[str] = frozenset(),
    ) -> None:
        self.files = dict(files or {})
        self.refused = refused
        self.unavailable = set(unavailable)
        self.fetched: list[str] = []

    async def fetch(self, file_id: str) -> bytes:
        self.fetched.append(file_id)
        if file_id in self.refused:
            raise ChannelRefusedError(REFUSAL)
        if file_id in self.unavailable:
            self.unavailable.discard(file_id)
            raise ChannelUnavailableError(OUTAGE)
        return self.files[file_id]


class FakeSketcher:
    """Draws by writing a PNG to `made`, or fails with `error` when it is set."""

    def __init__(self, made: Path, *, error: str | None = None) -> None:
        self.made = made
        self.error = error
        self.descriptions: list[str] = []

    async def draw(self, description: str) -> Path:
        self.descriptions.append(description)
        if self.error is not None:
            raise SketchError(self.error)
        self.made.parent.mkdir(parents=True, exist_ok=True)
        self.made.write_bytes(PNG)
        return self.made
