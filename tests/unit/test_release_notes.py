from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Final

import pytest
from scripts import release_notes

if TYPE_CHECKING:
    from pathlib import Path

REPO: Final = "example/shikiri-yaku"
IDENTITY: Final = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit(repo: Path, subject: str, body: str = "") -> str:
    extra = ["-m", body] if body else []
    _git(repo, "commit", "--allow-empty", "-q", "-m", subject, *extra)
    return _git(repo, "rev-parse", "HEAD")


def _commit_line(sha: str, subject: str) -> str:
    return f"* [{sha[:7]}](https://github.com/{REPO}/commit/{sha}): {subject}"


@pytest.mark.parametrize(
    ("previous", "tag", "expected"),
    [
        (["v0.1.9", "v0.1.10"], "v0.1.11", "v0.1.10"),
        (["v0.1.9", "v0.1.10"], "v0.1.10", "v0.1.9"),
        (["v0.9.0", "v0.10.0"], "v0.11.0", "v0.10.0"),
        (["v0.1.10", "v0.1.9"], "v0.1.9", None),
        (["v0.1", "nightly", "v0.2.0"], "v0.2.1", "v0.2.0"),
        ([], "v1.0.0", None),
    ],
)
def test_the_previous_tag_is_the_highest_lower_version(
    previous: list[str],
    tag: str,
    expected: str | None,
) -> None:
    assert release_notes.previous_tag(previous, tag) == expected


@pytest.mark.parametrize(
    ("subject", "body", "section"),
    [
        ("feat: add a restart verb", "", release_notes.FEATURES),
        ("feat(cli): add a restart verb", "", release_notes.FEATURES),
        ("fix: keep the topic", "", release_notes.FIXES),
        ("docs: explain the approval boundary", "", release_notes.OTHER),
        ("chore: bump the dependencies", "", release_notes.OTHER),
        ("Update the README", "", release_notes.OTHER),
        ("feat(cli)!: rename the flag", "", release_notes.BREAKING),
        ("fix: keep the topic", "BREAKING CHANGE: the topic moved.", release_notes.BREAKING),
        ("Rename the flag", "BREAKING CHANGE: the flag moved.", release_notes.BREAKING),
    ],
)
def test_a_commit_lands_in_its_section(
    repo: Path,
    subject: str,
    body: str,
    section: str,
) -> None:
    sha = _commit(repo, subject, body)
    _git(repo, "tag", "v0.1.0")

    text = release_notes.notes("v0.1.0", REPO)

    assert text.count(subject) == 1
    assert f"### {section}\n\n{_commit_line(sha, subject)}\n" in text


@pytest.mark.parametrize(
    ("subjects", "headings"),
    [
        (
            ["docs: x", "fix: y", "feat: z", "feat!: w"],
            ["### Breaking changes", "### Features", "### Fixes", "### Other changes"],
        ),
        (["feat: z", "docs: x"], ["### Features", "### Other changes"]),
        (["fix: y"], ["### Fixes"]),
    ],
)
def test_sections_come_in_order_and_empty_ones_are_omitted(
    repo: Path,
    subjects: list[str],
    headings: list[str],
) -> None:
    for subject in subjects:
        _commit(repo, subject)
    _git(repo, "tag", "v0.1.0")

    text = release_notes.notes("v0.1.0", REPO)
    found = [line for line in text.splitlines() if line.startswith("### ")]

    assert found == [*headings, "### Install"]


def test_the_first_release_lists_every_commit_up_to_its_tag(repo: Path) -> None:
    first = _commit(repo, "feat: the first feature")
    _git(repo, "tag", "v0.1.0")
    _commit(repo, "fix: after the tag")

    text = release_notes.notes("v0.1.0", REPO)

    assert _commit_line(first, "feat: the first feature") in text
    assert "after the tag" not in text
    assert "Full Changelog" not in text


def test_notes_start_after_the_previous_tag_by_number(repo: Path) -> None:
    _commit(repo, "fix: before the previous tag")
    _git(repo, "tag", "v0.1.9")
    _commit(repo, "feat: between the tags")
    _git(repo, "tag", "v0.1.10")
    _commit(repo, "fix: after the previous tag")
    _git(repo, "tag", "v0.1.11")

    text = release_notes.notes("v0.1.11", REPO)

    assert "after the previous tag" in text
    assert "between the tags" not in text
    assert "before the previous tag" not in text
    assert f"[v0.1.10...v0.1.11](https://github.com/{REPO}/compare/v0.1.10...v0.1.11)" in text


def test_the_links_name_the_compare_view_and_the_pinned_install(repo: Path) -> None:
    _commit(repo, "fix: the old one")
    _git(repo, "tag", "v0.1.0")
    new = _commit(repo, "fix: the new one")
    _git(repo, "tag", "v0.1.1")

    text = release_notes.notes("v0.1.1", REPO)

    assert _commit_line(new, "fix: the new one") in text
    assert (
        f"**Full Changelog**: [v0.1.0...v0.1.1](https://github.com/{REPO}/compare/v0.1.0...v0.1.1)"
    ) in text
    assert "### Install\n\n```sh\nuv tool install shikiri-yaku==0.1.1\n```" in text


def test_the_commit_option_lists_up_to_that_commit_before_the_tag_exists(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(repo, "fix: the old one")
    _git(repo, "tag", "v0.1.0")
    new = _commit(repo, "feat: the new one")
    _commit(repo, "fix: after the new one")

    assert release_notes.main(["v0.1.1", "--commit", new, "--repo", REPO]) == 0

    text = capsys.readouterr().out
    assert _commit_line(new, "feat: the new one") in text
    assert "after the new one" not in text
    assert "the old one" not in text
    assert "uv tool install shikiri-yaku==0.1.1" in text
    assert "v0.1.1" not in _git(repo, "tag", "--list")


def test_main_reads_the_repository_from_the_environment(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(repo, "fix: the only one")
    _git(repo, "tag", "v0.1.0")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)

    assert release_notes.main(["v0.1.0"]) == 0
    assert f"https://github.com/{REPO}/commit/" in capsys.readouterr().out


def test_main_fails_without_a_repository(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(repo, "fix: the only one")
    _git(repo, "tag", "v0.1.0")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert release_notes.main(["v0.1.0"]) == release_notes.FAILED
    assert "--repo" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ["0.1.1", "v0.1", "release-1"])
def test_a_tag_that_is_not_a_release_version_is_refused(tag: str) -> None:
    with pytest.raises(release_notes.NotesError):
        release_notes.notes(tag, REPO)
