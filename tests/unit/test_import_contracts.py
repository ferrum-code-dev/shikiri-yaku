from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LINTER = Path(sys.executable).with_name("lint-imports")
_CLEAN_EXIT = 0
_BROKEN_EXIT = 1


def _write_project(root: Path, leak: str | None) -> None:
    shutil.copy(_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copytree(
        _ROOT / "src" / "shikiri_yaku",
        root / "shikiri_yaku",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if leak is not None:
        (root / "shikiri_yaku" / "domain" / "leak.py").write_text(
            f"import shikiri_yaku.{leak}\n", encoding="utf-8"
        )


def _lint_exit_code(root: Path) -> int:
    return subprocess.run([_LINTER], cwd=root, capture_output=True, check=False).returncode


def test_the_layout_as_written_keeps_every_contract(tmp_path: Path) -> None:
    _write_project(tmp_path, leak=None)

    assert _lint_exit_code(tmp_path) == _CLEAN_EXIT


@pytest.mark.parametrize("outer", ["app", "adapters", "journal"])
def test_domain_importing_an_outer_package_breaks_the_contract(tmp_path: Path, outer: str) -> None:
    _write_project(tmp_path, leak=outer)

    assert _lint_exit_code(tmp_path) == _BROKEN_EXIT
