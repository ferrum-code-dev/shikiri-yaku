"""The files of an Expert conversation: what the PO attaches or a sketch makes, and their copies."""

from __future__ import annotations

import re
import shutil
from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.settings import EXPERT_FILE_NAME_MAX_CHARS

if TYPE_CHECKING:
    from pathlib import Path

FILES_DIR: Final = "files"
TASK_FILES_DIR: Final = "task_files"
# keep: an attachment always starts with its update id's digits, so no attachment takes this name
SKETCH_PREFIX: Final = "sketch"
# keep: the name of a file is no path; anything but these characters, and leading dots, go
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]")
_FALLBACK_NAME: Final = "file"


def attachment_name(update_id: int, name: str) -> str:
    """Return the name a file is kept under: its update id, then the name's safe characters."""
    stem = _UNSAFE.sub("_", name).lstrip(".")[:EXPERT_FILE_NAME_MAX_CHARS]
    return f"{update_id}-{stem or _FALLBACK_NAME}"


def save_attachment(directory: Path, update_id: int, name: str, content: bytes) -> Path:
    """Write an attachment into the conversation's files and return its absolute path."""
    folder = directory / FILES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / attachment_name(update_id, name)
    path.write_bytes(content)
    return path.absolute()


def move_sketch(directory: Path, update_id: int, source: Path) -> Path:
    """Move a drawn picture into the conversation's files and return its absolute path."""
    folder = directory / FILES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{SKETCH_PREFIX}-{update_id}.png"
    shutil.move(source, target)
    return target.absolute()


class TaskFiles:
    """Copies a conversation's files for a proposal's tasks, since the conversation may end first.

    The copies stay when the conversation's files are removed, so a task that runs later still
    finds them.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def copy_for_proposal(self, conversation_id: int, proposal_id: int) -> list[str]:
        """Copy every file of the conversation under the proposal; return the absolute copies.

        An OSError from a copy is raised, and then no task of the proposal is submitted.
        """
        folder = conversation_directory(self._directory, conversation_id) / FILES_DIR
        if not folder.is_dir():
            return []
        sources = sorted(path for path in folder.iterdir() if path.is_file())
        if not sources:
            return []
        target = self._directory / TASK_FILES_DIR / str(proposal_id)
        target.mkdir(parents=True, exist_ok=True)
        copies: list[str] = []
        for source in sources:
            copy = target / source.name
            shutil.copy2(source, copy)
            copies.append(str(copy.absolute()))
        return copies
