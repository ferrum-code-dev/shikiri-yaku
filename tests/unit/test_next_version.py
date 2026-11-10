from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from scripts import next_version

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT: Final = Path(__file__).resolve().parents[2] / "scripts" / "next_version.py"
SHA: Final = "a" * 40
IDENTITY: Final = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _commit(subject: str, body: str = "") -> next_version.release_notes.Commit:
    return next_version.release_notes.Commit(SHA, subject, body)


@pytest.mark.parametrize(
    ("last", "messages", "expected"),
    [
        ((0, 0, 0), [("docs: explain the approval boundary", "")], None),
        ((0, 0, 0), [("test: cover the parser", ""), ("chore: bump", "")], None),
        ((0, 0, 0), [("Update the README", "")], None),
        ((0, 0, 0), [("fix: keep the topic", "")], (0, 0, 1)),
        ((0, 0, 0), [("feat: add a restart verb", "")], (0, 0, 1)),
        ((0, 0, 0), [("feat!: rename the flag", "")], (0, 1, 0)),
        ((0, 2, 3), [("docs: x", ""), ("fix: y", "")], (0, 2, 4)),
        ((0, 2, 3), [("perf: faster lookup", "")], (0, 2, 4)),
        ((0, 2, 3), [("feat: z", "")], (0, 2, 4)),
        ((0, 2, 3), [("fix: keep", "BREAKING CHANGE: the topic moved.")], (0, 3, 0)),
        ((0, 2, 3), [("fix: y", ""), ("refactor(x)!: w", "")], (0, 3, 0)),
        ((1, 2, 3), [("docs: x", "")], None),
        ((1, 2, 3), [("fix: y", "")], (1, 2, 4)),
        ((1, 2, 3), [("perf: y", "")], (1, 2, 4)),
        ((1, 2, 3), [("feat: z", ""), ("fix: y", "")], (1, 3, 0)),
        ((1, 2, 3), [("feat!: w", "")], (2, 0, 0)),
        ((1, 2, 3), [("fix: y", "BREAKING CHANGE: moved.")], (2, 0, 0)),
    ],
)
def test_the_proposal_follows_the_bump_rule(
    last: tuple[int, int, int],
    messages: Sequence[tuple[str, str]],
    expected: tuple[int, int, int] | None,
) -> None:
    commits = [_commit(subject, body) for subject, body in messages]

    assert next_version.bump(last, commits) == expected


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit_in(repo: Path, subject: str) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_prints_the_proposal_for_the_commit(repo: Path) -> None:
    _commit_in(repo, "fix: the first fix")
    _git(repo, "tag", "v0.1.0")
    head = _commit_in(repo, "feat!: rename the flag")

    done = _cli(repo, "--commit", head)

    assert (done.returncode, done.stdout) == (0, "0.2.0\n")


def test_the_cli_says_nothing_to_release_and_exits_two(repo: Path) -> None:
    _commit_in(repo, "fix: the first fix")
    _git(repo, "tag", "v0.1.0")
    head = _commit_in(repo, "docs: explain the release")

    done = _cli(repo, "--commit", head)

    assert (done.returncode, done.stderr.strip()) == (2, "nothing to release")


def test_the_version_option_replaces_the_proposal_without_a_releasable_commit(repo: Path) -> None:
    _commit_in(repo, "fix: the first fix")
    _git(repo, "tag", "v0.1.0")
    head = _commit_in(repo, "docs: explain the release")

    done = _cli(repo, "--commit", head, "--version", "0.1.5")

    assert (done.returncode, done.stdout) == (0, "0.1.5\n")


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9", "1.2", "v0.2.0"])
def test_the_version_option_refuses_a_version_that_is_not_higher_or_not_valid(
    repo: Path,
    version: str,
) -> None:
    _commit_in(repo, "fix: the first fix")
    _git(repo, "tag", "v0.1.0")
    head = _commit_in(repo, "feat: a feature")

    done = _cli(repo, "--commit", head, "--version", version)

    assert done.returncode == 1
    assert "next_version:" in done.stderr


def test_the_version_option_refuses_a_tag_that_already_exists(repo: Path) -> None:
    _commit_in(repo, "fix: the first fix")
    _git(repo, "tag", "v0.1.0")
    earlier = _commit_in(repo, "feat: a feature")
    later = _commit_in(repo, "fix: a later fix")
    _git(repo, "tag", "v0.5.0", later)

    done = _cli(repo, "--commit", earlier, "--version", "0.5.0")

    assert done.returncode == 1
    assert "the tag v0.5.0 already exists" in done.stderr
