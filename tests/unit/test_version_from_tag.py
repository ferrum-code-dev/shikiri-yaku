"""A wheel is named for the version it is built with: the tag, or the release's proposed version."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
TAG: Final = "v9.9.9"
PROPOSED: Final = "8.8.8"
PRETEND: Final = "SETUPTOOLS_SCM_PRETEND_VERSION"
IDENTITY: Final = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _git(cwd: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def _copy_working_tree(target: Path) -> None:
    # keep: the clone must include working-tree changes while the gate runs before commit
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    for raw in filter(None, listed.split(b"\0")):
        name = raw.decode()
        source = REPO / name
        if source.is_file():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)


def _snapshot(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    cloned = subprocess.run(
        ["git", "clone", "--local", str(REPO), str(checkout)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    _copy_working_tree(checkout)
    _git(checkout, "add", "-A")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status:
        _git(checkout, "commit", "-q", "-m", "snapshot")
    return checkout


def _build(cwd: Path, command: str, out: Path, pretend: str | None = None) -> None:
    # keep: the build must not inherit a version from the environment that runs the gate
    env = {key: value for key, value in os.environ.items() if key != PRETEND}
    if pretend is not None:
        env[PRETEND] = pretend
    done = subprocess.run(
        [
            "uv",
            "--offline",
            "build",
            command,
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(out),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_wheel_built_from_a_version_tag_carries_the_tag_version(tmp_path: Path) -> None:
    checkout = _snapshot(tmp_path)
    _git(checkout, "tag", TAG)
    wheel_dist = tmp_path / "wheel-dist"

    # keep: no network, and the test's own hatchling and hatch-vcs build the wheel
    _build(checkout, "--wheel", wheel_dist)

    (wheel,) = wheel_dist.glob("shikiri_yaku-*.whl")
    assert wheel.name.startswith("shikiri_yaku-9.9.9-")

    sdist_dist = tmp_path / "sdist-dist"
    _build(checkout, "--sdist", sdist_dist)

    (sdist,) = sdist_dist.glob("shikiri_yaku-*.tar.gz")
    extracted = tmp_path / "sdist-source"
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    (sdist_checkout,) = extracted.iterdir()
    assert not (sdist_checkout / ".git").exists()

    sdist_wheel_dist = tmp_path / "sdist-wheel-dist"
    _build(sdist_checkout, "--wheel", sdist_wheel_dist)

    (sdist_wheel,) = sdist_wheel_dist.glob("shikiri_yaku-*.whl")
    assert sdist_wheel.name.startswith("shikiri_yaku-9.9.9-")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_wheel_built_before_the_tag_exists_carries_the_proposed_version(tmp_path: Path) -> None:
    checkout = _snapshot(tmp_path)
    wheel_dist = tmp_path / "wheel-dist"

    _build(checkout, "--wheel", wheel_dist, pretend=PROPOSED)

    (wheel,) = wheel_dist.glob("shikiri_yaku-*.whl")
    assert wheel.name == f"shikiri_yaku-{PROPOSED}-py3-none-any.whl"
