from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import pytest
from scripts import release

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

OLD: Final = "0.1.9"
NEW: Final = "0.1.10"
SHA: Final = "9ec6819d2f0b4c1e8a7f3b5d6e9c0a1b2c3d4e5f"
OTHER_SHA: Final = "1111111111111111111111111111111111111111"
PROJECT_FILE: Final = "pyproject.toml"
LOCK_FILE: Final = "uv.lock"
PYPROJECT: Final = '[project]\nname = "shikiri-yaku"\ndynamic = ["version"]\n'
LOCK: Final = (
    '[[package]]\nname = "other"\nversion = "0.1.9"\n\n'
    '[[package]]\nname = "shikiri-yaku"\nsource = { editable = "." }\n'
)
README: Final = (
    f"uv tool install https://github.com/ferrum-code-dev/shikiri-yaku/releases/download/v{OLD}/"
    f"shikiri_yaku-{OLD}-py3-none-any.whl\n"
    f"The version in these addresses is `{OLD}`. Plain 11.0.1.9 and 0.1.90 are not it.\n"
    f"Version {OLD}.\n"
)
MUTATING: Final = ("add", "commit", "push")
RELEASE_SEQUENCE: Final = (
    ("git", "add", "README.md"),
    ("git", "commit", "-m", "chore: release 0.1.10", "-m", release.CO_AUTHOR),
    ("git", "push", "origin", "main"),
    ("git", "tag", "v0.1.10"),
    ("git", "push", "origin", "v0.1.10"),
)


@dataclass
class FakeGit:
    """Answers the git commands the release runs, and records every command in order."""

    branch: str = "main"
    status: str = ""
    head: str = SHA
    origin_main: str = SHA
    version_tags: str = f"v{OLD}\n"
    tags: str = ""
    failing: tuple[str, ...] = ()
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], _cwd: Path) -> release.CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        if command == self.failing:
            return release.CommandResult(1, "", "refused by the fake")
        return release.CommandResult(0, self._answer(command), "")

    def _answer(self, command: tuple[str, ...]) -> str:
        if command == release.VERSION_TAGS_ARGV:
            return self.version_tags
        if command[:3] == ("git", "tag", "--list"):
            return self.tags
        answers: dict[tuple[str, ...], str] = {
            release.BRANCH_ARGV: self.branch,
            release.STATUS_ARGV: self.status,
            release.HEAD_ARGV: self.head,
            release.ORIGIN_MAIN_ARGV: self.origin_main,
        }
        return answers.get(command, "")

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            call
            for call in self.calls
            if call[1] in MUTATING or (call[:2] == ("git", "tag") and "--list" not in call)
        ]


def _write_checkout(path: Path) -> None:
    (path / PROJECT_FILE).write_text(PYPROJECT, encoding="utf-8")
    (path / LOCK_FILE).write_text(LOCK, encoding="utf-8")
    (path / release.README_FILE).write_text(README, encoding="utf-8")


def _read_checkout(path: Path) -> tuple[str, str, str]:
    return (
        (path / PROJECT_FILE).read_text(encoding="utf-8"),
        (path / LOCK_FILE).read_text(encoding="utf-8"),
        (path / release.README_FILE).read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        ("patch", (0, 1, 10)),
        ("minor", (0, 2, 0)),
        ("major", (1, 0, 0)),
        ("0.1.12", (0, 1, 12)),
        ("1.0.0", (1, 0, 0)),
    ],
)
def test_a_request_names_the_next_version(request_: str, expected: release.Version) -> None:
    assert release.next_version((0, 1, 9), request_) == expected


@pytest.mark.parametrize("request_", ["0.1.9", "0.1.8", "0.0.1", "0.2", "v0.2.0", "latest"])
def test_an_explicit_version_must_be_a_newer_x_y_z(request_: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.next_version((0, 1, 9), request_)


@pytest.mark.parametrize(
    ("git", "request_"),
    [
        (FakeGit(branch="feature/x"), "patch"),
        (FakeGit(branch=""), "patch"),
        (FakeGit(status=" M README.md\n"), "patch"),
        (FakeGit(head=OTHER_SHA), "patch"),
        (FakeGit(tags=f"v{NEW}\n"), "patch"),
        (FakeGit(), OLD),
        (FakeGit(), "not-a-version"),
    ],
)
def test_a_refused_release_commits_pushes_and_tags_nothing(
    tmp_path: Path, git: FakeGit, request_: str
) -> None:
    _write_checkout(tmp_path)
    before = _read_checkout(tmp_path)

    code = release.release(git, tmp_path, request_, dry_run=False)

    assert code == release.FAILED
    assert git.mutations() == []
    assert _read_checkout(tmp_path) == before


def test_a_release_uses_the_tag_then_updates_release_references_and_tags(tmp_path: Path) -> None:
    _write_checkout(tmp_path)
    git = FakeGit()

    code = release.release(git, tmp_path, "patch", dry_run=False)

    assert code == 0
    assert tuple(git.mutations()) == RELEASE_SEQUENCE
    project, lock, readme = _read_checkout(tmp_path)
    assert project == PYPROJECT
    assert lock == LOCK
    assert f"v{NEW}/shikiri_yaku-{NEW}-py3" in readme


def test_a_dry_run_runs_the_checks_and_changes_nothing(tmp_path: Path) -> None:
    _write_checkout(tmp_path)
    before = _read_checkout(tmp_path)
    git = FakeGit()

    code = release.release(git, tmp_path, "minor", dry_run=True)

    assert code == 0
    assert git.mutations() == []
    assert _read_checkout(tmp_path) == before
    assert release.FETCH_ARGV in git.calls


def test_a_failed_push_stops_before_the_tag(tmp_path: Path) -> None:
    _write_checkout(tmp_path)
    git = FakeGit(failing=("git", "push", "origin", "main"))

    code = release.release(git, tmp_path, "patch", dry_run=False)

    assert code == release.FAILED
    assert tuple(git.mutations()) == RELEASE_SEQUENCE[:3]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"releases/download/v{OLD}/x-{OLD}-py3", f"releases/download/v{NEW}/x-{NEW}-py3"),
        (f"The version is `{OLD}`.", f"The version is `{NEW}`."),
        (f"Version {OLD}.", f"Version {NEW}."),
        ("11.0.1.9 and 0.1.90 and 10.0.1.9", "11.0.1.9 and 0.1.90 and 10.0.1.9"),
    ],
)
def test_the_readme_changes_whole_token_versions_only(text: str, expected: str) -> None:
    assert release.set_readme_versions(text, OLD, NEW) == expected
