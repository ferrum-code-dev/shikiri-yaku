"""The files of an install run under the instance directory: a scratch directory and the output.

The instance directory is no repository, so a command that starts in the scratch directory starts
outside every repository. The output file is named for the op, so one run has one file. A package
manager may print paths or tokens there, so the output is readable by its owner only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.instance import OUTPUT_DIR_NAME

if TYPE_CHECKING:
    from pathlib import Path

SCRATCH_DIR_NAME: Final = "install-scratch"
OUTPUT_DIR_MODE: Final = 0o700
OUTPUT_FILE_MODE: Final = 0o600


class InstanceInstallFiles:
    """Keeps the scratch directory and the output files under the instance directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def scratch_dir(self) -> Path:
        path = self._directory / SCRATCH_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_output(self, op_id: int, text: str) -> str:
        # keep: chmod too, because mkdir and touch leave an existing directory or file as it was
        folder = self._directory / OUTPUT_DIR_NAME
        folder.mkdir(mode=OUTPUT_DIR_MODE, parents=True, exist_ok=True)
        folder.chmod(OUTPUT_DIR_MODE)
        path = folder / f"install-{op_id}.log"
        path.touch(mode=OUTPUT_FILE_MODE, exist_ok=True)
        path.chmod(OUTPUT_FILE_MODE)
        path.write_text(text, encoding="utf-8")
        return str(path)
