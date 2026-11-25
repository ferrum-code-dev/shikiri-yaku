"""Conversation files: the names attachments are kept under, and the copies that outlive them."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

import pytest

from shikiri_yaku.app.expert_files import (
    TaskFiles,
    attachment_name,
    move_sketch,
    save_attachment,
)
from shikiri_yaku.settings import EXPERT_FILE_NAME_MAX_CHARS

UPDATE: Final = 7
HOSTILE_NAMES: Final = ("../../etc/passwd", "/abs/x.txt", "..", "...", "", "AGENTS.md", "a\x00b")


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("photo.jpg", "7-photo.jpg"),
        ("AGENTS.md", "7-AGENTS.md"),
        ("../../etc/passwd", "7-_.._etc_passwd"),
        ("/abs/x.txt", "7-_abs_x.txt"),
        ("...", "7-file"),
        ("", "7-file"),
        ("a b é.pdf", "7-a_b__.pdf"),
    ],
)
def test_an_attachment_is_kept_under_its_update_id_and_safe_characters(
    name: str, kept: str
) -> None:
    assert attachment_name(UPDATE, name) == kept


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_no_attachment_name_leaves_its_directory_or_hides(name: str) -> None:
    kept = attachment_name(UPDATE, name)

    assert Path(kept).name == kept
    assert not kept.startswith(".")


def test_a_long_name_is_cut_to_the_limit() -> None:
    kept = attachment_name(UPDATE, "a" * (EXPERT_FILE_NAME_MAX_CHARS * 2))

    assert kept == f"7-{'a' * EXPERT_FILE_NAME_MAX_CHARS}"


def test_an_attachment_is_saved_under_the_conversation_files(tmp_path: Path) -> None:
    saved = save_attachment(tmp_path, UPDATE, "photo.jpg", b"picture")

    assert saved == tmp_path / "files" / "7-photo.jpg"
    assert saved.is_absolute()
    assert saved.read_bytes() == b"picture"


def test_a_drawn_sketch_is_moved_into_the_conversation_files(tmp_path: Path) -> None:
    drawn = tmp_path / "codex" / "ig.png"
    drawn.parent.mkdir()
    drawn.write_bytes(b"sketch")

    moved = move_sketch(tmp_path / "conversation", UPDATE, drawn)

    assert moved == tmp_path / "conversation" / "files" / "sketch-7.png"
    assert moved.read_bytes() == b"sketch"
    assert not drawn.exists()


def test_task_copies_outlive_the_conversation_directory(tmp_path: Path) -> None:
    files = tmp_path / "conversations" / "3" / "files"
    files.mkdir(parents=True)
    (files / "7-photo.jpg").write_bytes(b"photo")

    copies = TaskFiles(tmp_path).copy_for_proposal(3, 9)
    shutil.rmtree(tmp_path / "conversations" / "3")

    assert copies == [str(tmp_path / "task_files" / "9" / "7-photo.jpg")]
    assert [Path(copy).read_bytes() for copy in copies] == [b"photo"]


def test_a_conversation_without_files_copies_nothing(tmp_path: Path) -> None:
    assert TaskFiles(tmp_path).copy_for_proposal(3, 9) == []
    assert not (tmp_path / "task_files").exists()
