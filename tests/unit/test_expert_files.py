"""Offline file receipt with a real journal and a fake channel."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from shikiri_yaku.app.expert_files import ExpertFiles
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.domain.inbound import Attachment
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.ports import ChannelUnavailableError

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _write(target: Path, content: bytes) -> None:
    target.write_bytes(content)


class Download:
    def __init__(self, content: bytes, *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls = 0

    async def download(self, file_id: str, target: Path, max_bytes: int) -> int:
        del file_id, max_bytes
        self.calls += 1
        _write(target, self.content)
        if self.fail:
            message = "failed"
            raise ChannelUnavailableError(message)
        return len(self.content)


@pytest.mark.parametrize(
    ("name", "content", "accepted"),
    [
        ("a.txt", b"hello", True),
        ("a.md", b"# heading", True),
        ("a.csv", b"a,b\n1,2", True),
        ("a.json", b'{"a":1}', True),
        ("a.json", b"not json", False),
        ("a.txt", b"\xff", False),
        ("a.png", b"image", False),
        ("a.zip", b"archive", False),
    ],
)
def test_receive_validates_and_deduplicates(
    tmp_path: Path,
    name: str,
    content: bytes,
    accepted: bool,  # noqa: FBT001 -- parametrized
) -> None:
    database = Database.open(":memory:")
    log = Conversations(database)
    log.create(1, chat=10, thread=None, text="hello", scope=[], now=NOW)
    conversation = log.get(1)
    assert conversation is not None
    downloader = Download(content)
    files = ExpertFiles(log, downloader, tmp_path, max_file=100, max_total=200)
    attachment = Attachment(file_id="file", name=name)
    assert asyncio.run(files.receive(conversation.id, 2, attachment)) is accepted
    assert asyncio.run(files.receive(conversation.id, 2, attachment)) is accepted
    stored = list(conversation_directory(tmp_path, conversation.id).glob("*"))
    assert len(stored) == int(accepted)
    if accepted:
        assert stored[0].read_bytes() == content
    assert downloader.calls <= 1
    database.close()


def test_interrupted_reservation_retries_and_failure_removes_partial(tmp_path: Path) -> None:
    database = Database.open(":memory:")
    log = Conversations(database)
    log.create(1, chat=10, thread=None, text="hello", scope=[], now=NOW)
    conversation = log.get(1)
    assert conversation is not None
    name, _, _ = log.reserve_attachment(conversation.id, 2, 100, 100) or ("", "", 0)
    directory = conversation_directory(tmp_path, conversation.id)
    directory.mkdir(parents=True)
    (directory / (name + ".part")).write_bytes(b"old")
    downloader = Download(b"partial", fail=True)
    files = ExpertFiles(log, downloader, tmp_path, max_file=100, max_total=100)
    assert not asyncio.run(files.receive(conversation.id, 2, Attachment(file_id="a", name="a.txt")))
    assert list(directory.iterdir()) == []
    assert not asyncio.run(files.receive(conversation.id, 2, Attachment(file_id="a", name="a.txt")))
    assert downloader.calls == 1
    database.close()


def test_reservation_limits_total_and_recovers_partial(tmp_path: Path) -> None:
    database = Database.open(":memory:")
    log = Conversations(database)
    log.create(1, chat=10, thread=None, text="hello", scope=[], now=NOW)
    conversation = log.get(1)
    assert conversation is not None
    row = log.reserve_attachment(conversation.id, 2, 10, 12)
    assert row is not None
    name, _, _ = row
    assert log.reserve_attachment(conversation.id, 3, 10, 12) is not None
    assert log.reserve_attachment(conversation.id, 4, 10, 12) is None
    directory = conversation_directory(tmp_path, conversation.id)
    directory.mkdir(parents=True)
    (directory / (name + ".part")).write_bytes(b"old")
    downloader = Download(b"new")
    files = ExpertFiles(log, downloader, tmp_path, max_file=10, max_total=12)
    assert asyncio.run(files.receive(conversation.id, 2, Attachment(file_id="a", name="a.txt")))
    assert (directory / name).read_bytes() == b"new"
    assert not (directory / (name + ".part")).exists()
    assert log.reserve_attachment(conversation.id, 4, 10, 12) is not None
    database.close()


@pytest.mark.parametrize(
    ("extension", "image_format"), [(".png", "PNG"), (".jpg", "JPEG"), (".jpeg", "JPEG")]
)
def test_image_receipt_requires_a_complete_matching_image(
    tmp_path: Path, extension: str, image_format: str
) -> None:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format=image_format)
    image = output.getvalue()
    database = Database.open(":memory:")
    log = Conversations(database)
    log.create(1, chat=10, thread=None, text="hello", scope=[], now=NOW)
    conversation = log.get(1)
    assert conversation is not None
    good = ExpertFiles(log, Download(image), tmp_path, max_file=10000, max_total=50000)
    assert asyncio.run(
        good.receive(conversation.id, 2, Attachment(file_id="a", name="a" + extension))
    )
    truncated = ExpertFiles(
        log, Download(image[: len(image) // 2]), tmp_path, max_file=10000, max_total=50000
    )
    assert not asyncio.run(
        truncated.receive(conversation.id, 3, Attachment(file_id="b", name="b" + extension))
    )
    wrong_extension = ".jpg" if image_format == "PNG" else ".png"
    assert not asyncio.run(
        good.receive(conversation.id, 4, Attachment(file_id="c", name="c" + wrong_extension))
    )
    assert len(list(conversation_directory(tmp_path, conversation.id).iterdir())) == 1
    database.close()
