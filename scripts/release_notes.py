"""Prints the notes of a release from the commits since the previous release tag.

`uv run python scripts/release_notes.py <tag> [--repo <owner/name>]` lists the commits from the
previous `v<major>.<minor>.<patch>` tag up to `<tag>`, without merges and without the
`chore: release` commits. The previous tag is the highest one whose numbers are below the tag's, so
`v0.1.10` comes after `v0.1.9`. The notes group the commits as Breaking changes, Features, Fixes and
Other changes, then link the previous tag's compare view and give the install command. `--repo`
defaults to `GITHUB_REPOSITORY`. The release workflow passes the output to `gh release create`. The
script uses the standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ENV: Final = "GITHUB_REPOSITORY"
DISTRIBUTION: Final = "shikiri_yaku"
RELEASE_SUBJECT_PREFIX: Final = "chore: release"
BREAKING_LINE_PREFIX: Final = "BREAKING CHANGE:"
LOG_FORMAT: Final = "--format=%H%x1f%s%x1f%b%x1e"
FIELD_SEPARATOR: Final = "\x1f"
RECORD_SEPARATOR: Final = "\x1e"
FIELD_COUNT: Final = 3
SHORT_SHA_LENGTH: Final = 7
TAG_PATTERN: Final = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SUBJECT_PATTERN: Final = re.compile(r"^(?P<type>[a-z]+)(?:\([^()]*\))?(?P<bang>!)?: .+")
BREAKING: Final = "Breaking changes"
FEATURES: Final = "Features"
FIXES: Final = "Fixes"
OTHER: Final = "Other changes"
SECTION_ORDER: Final = (BREAKING, FEATURES, FIXES, OTHER)
SECTION_BY_TYPE: Final = {"feat": FEATURES, "fix": FIXES}
FAILED: Final = 1


class NotesError(Exception):
    """The release notes cannot be written for the requested tag."""


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


type Version = tuple[int, int, int]


def version_of(tag: str) -> Version | None:
    match = TAG_PATTERN.match(tag)
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def previous_tag(tags: Sequence[str], tag: str) -> str | None:
    current = version_of(tag)
    if current is None:
        return None
    earlier = [
        (version, name)
        for name in tags
        if (version := version_of(name)) is not None and version < current
    ]
    if not earlier:
        return None
    return max(earlier)[1]


def is_breaking(commit: Commit) -> bool:
    match = SUBJECT_PATTERN.match(commit.subject)
    if match is not None and match["bang"]:
        return True
    return any(line.startswith(BREAKING_LINE_PREFIX) for line in commit.body.splitlines())


def section_of(commit: Commit) -> str:
    if is_breaking(commit):
        return BREAKING
    match = SUBJECT_PATTERN.match(commit.subject)
    if match is None:
        return OTHER
    return SECTION_BY_TYPE.get(match["type"], OTHER)


def commit_line(commit: Commit, repo: str) -> str:
    short = commit.sha[:SHORT_SHA_LENGTH]
    return f"* [{short}](https://github.com/{repo}/commit/{commit.sha}): {commit.subject}"


def render(tag: str, repo: str, previous: str | None, commits: Sequence[Commit]) -> str:
    version = tag.removeprefix("v")
    download = f"https://github.com/{repo}/releases/download/{tag}"
    wheel = f"{download}/{DISTRIBUTION}-{version}-py3-none-any.whl"
    lines: list[str] = []
    for section in SECTION_ORDER:
        entries = [commit_line(commit, repo) for commit in commits if section_of(commit) == section]
        if entries:
            lines.extend([f"### {section}", "", *entries, ""])
    if previous is not None:
        compare = f"https://github.com/{repo}/compare/{previous}...{tag}"
        lines.extend([f"**Full Changelog**: [{previous}...{tag}]({compare})", ""])
    lines.extend(["### Install", "", "```sh", f"uv tool install {wheel}", "```"])
    return "\n".join(lines) + "\n"


def _git(*args: str) -> str:
    done = subprocess.run(("git", *args), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        message = f"git {' '.join(args)} failed: {done.stderr.strip()}"
        raise NotesError(message)
    return done.stdout


def read_commits(revisions: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in _git("log", revisions, "--no-merges", LOG_FORMAT).split(RECORD_SEPARATOR):
        fields = record.lstrip("\n").split(FIELD_SEPARATOR, 2)
        if len(fields) != FIELD_COUNT:
            continue
        sha, subject, body = fields
        if not subject.startswith(RELEASE_SUBJECT_PREFIX):
            commits.append(Commit(sha, subject, body.strip()))
    return commits


def notes(tag: str, repo: str | None) -> str:
    if version_of(tag) is None:
        message = f"{tag} is not a release tag of the form v<major>.<minor>.<patch>"
        raise NotesError(message)
    if repo is None:
        message = f"no repository: pass --repo or set {REPO_ENV}"
        raise NotesError(message)
    previous = previous_tag(_git("tag", "--list").splitlines(), tag)
    revisions = tag if previous is None else f"{previous}..{tag}"
    return render(tag, repo, previous, read_commits(revisions))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the release notes of a tag.")
    parser.add_argument("tag")
    parser.add_argument("--repo", default=os.environ.get(REPO_ENV))
    args = parser.parse_args(argv)
    try:
        text = notes(args.tag, args.repo)
    except NotesError as error:
        sys.stderr.write(f"release_notes: {error}\n")
        return FAILED
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
