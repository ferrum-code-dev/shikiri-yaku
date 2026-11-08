"""The self-install check: this checkout's wheel installs offline into temporary directories.

It is the one package install the tests run (see AGENTS.md). Only uv's download cache is shared
with the host; offline runs read it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping

REPOSITORY: Final = Path(__file__).resolve().parents[2]
UV: Final = shutil.which("uv")
TIMEOUT_S: Final = 900
DEFAULT_CACHE: Final = Path.home() / ".cache" / "uv"


def _environment(root: Path, cache: Path) -> Mapping[str, str]:
    """The environment of every uv and shikiri-yaku call: every directory is under root."""
    bin_dir = root / "bin"
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    environment.update(
        {
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "UV_TOOL_DIR": str(root / "tools"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_CACHE_DIR": str(cache),
            "UV_OFFLINE": "1",
            "UV_PYTHON": sys.executable,
            "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
        }
    )
    return environment


def _run(argv: list[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=REPOSITORY,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_S,
    )


@pytest.mark.skipif(UV is None, reason="the self-install check needs uv")
def test_the_built_wheel_installs_offline_and_update_runs_from_it(tmp_path: Path) -> None:
    cache = Path(os.environ.get("UV_CACHE_DIR") or DEFAULT_CACHE)
    environment = _environment(tmp_path, cache)
    dist = tmp_path / "dist"

    built = _run([str(UV), "build", "--wheel", "--out-dir", str(dist)], environment)
    assert built.returncode == 0, built.stderr
    (wheel,) = dist.glob("shikiri_yaku-*.whl")
    version = wheel.name.split("-")[1]

    installed = _run([str(UV), "tool", "install", "--reinstall", str(wheel)], environment)
    assert installed.returncode == 0, installed.stderr
    program = str(tmp_path / "bin" / "shikiri-yaku")
    shown = _run([program, "--version"], environment)
    assert shown.stdout.split()[-1:] == [version]

    updated = _run([program, "update", "--wheel", str(wheel)], environment)
    store = tmp_path / "data" / "shikiri-yaku" / "releases"
    assert updated.returncode == 1
    assert "restart printed no product list" in updated.stderr
    assert (store / wheel.name).is_file()
    assert not (store / "current").exists()
