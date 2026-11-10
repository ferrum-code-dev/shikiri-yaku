"""Proposes the next release version from the commits since the last release.

`python3 scripts/next_version.py --commit <sha> [--version <x.y.z>]` prints the version without
the `v`. The last release is the highest `v<major>.<minor>.<patch>` tag on the history of `<sha>`;
with none the base is 0.0.0. Only releasable commits count: `feat`, `fix`, `perf`, and breaking
ones (a `!` after the type or a `BREAKING CHANGE:` line). With none, the script says
`nothing to release` and exits 2. `--version` replaces the proposal: it must be higher than the last
release and no tag of that name may exist. The script uses the standard library and the parsing of
`release_notes.py`, so it runs from this folder.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from enum import IntEnum
from typing import TYPE_CHECKING, Final

import release_notes

if TYPE_CHECKING:
    from collections.abc import Sequence

ZERO: Final = (0, 0, 0)
FEATURE_TYPE: Final = "feat"
FIX_TYPES: Final = frozenset({"fix", "perf"})
FAILED: Final = 1
NOTHING_TO_RELEASE: Final = 2


class Level(IntEnum):
    FIX = 1
    FEATURE = 2
    BREAKING = 3


class NothingToReleaseError(Exception):
    """No commit since the last release is releasable."""


def _git(*args: str) -> str:
    done = subprocess.run(("git", *args), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        message = f"git {' '.join(args)} failed: {done.stderr.strip()}"
        raise release_notes.NotesError(message)
    return done.stdout


def _text(version: release_notes.Version) -> str:
    return ".".join(str(part) for part in version)


def level_of(commit: release_notes.Commit) -> Level | None:
    if release_notes.is_breaking(commit):
        return Level.BREAKING
    match = release_notes.SUBJECT_PATTERN.match(commit.subject)
    if match is None:
        return None
    if match["type"] == FEATURE_TYPE:
        return Level.FEATURE
    if match["type"] in FIX_TYPES:
        return Level.FIX
    return None


def last_release(tags: Sequence[str]) -> tuple[release_notes.Version, str | None]:
    released = [
        (version, tag) for tag in tags if (version := release_notes.version_of(tag)) is not None
    ]
    if not released:
        return ZERO, None
    version, tag = max(released)
    return version, tag


def bump(
    last: release_notes.Version,
    commits: Sequence[release_notes.Commit],
) -> release_notes.Version | None:
    levels = [level for commit in commits if (level := level_of(commit)) is not None]
    if not levels:
        return None
    level = max(levels)
    major, minor, patch = last
    if major == 0:
        if level is Level.BREAKING:
            return (0, minor + 1, 0)
        return (0, minor, patch + 1)
    if level is Level.BREAKING:
        return (major + 1, 0, 0)
    if level is Level.FEATURE:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def requested_version(
    text: str,
    last: release_notes.Version,
    tags: Sequence[str],
) -> release_notes.Version:
    version = release_notes.version_of(f"v{text}")
    if version is None:
        message = f"{text} is not <major>.<minor>.<patch>"
        raise release_notes.NotesError(message)
    if version <= last:
        message = f"{text} is not higher than the last release {_text(last)}"
        raise release_notes.NotesError(message)
    if f"v{text}" in tags:
        message = f"the tag v{text} already exists"
        raise release_notes.NotesError(message)
    return version


def propose(sha: str, requested: str | None) -> release_notes.Version:
    last, tag = last_release(_git("tag", "--list", "--merged", sha).splitlines())
    if requested is not None:
        return requested_version(requested, last, _git("tag", "--list").splitlines())
    revisions = sha if tag is None else f"{tag}..{sha}"
    proposal = bump(last, release_notes.read_commits(revisions))
    if proposal is None:
        raise NothingToReleaseError
    return proposal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the next release version.")
    parser.add_argument("--commit", required=True, help="the commit the release is made from")
    parser.add_argument("--version", help="use this version instead of the proposal")
    args = parser.parse_args(argv)
    try:
        version = propose(args.commit, args.version)
    except NothingToReleaseError:
        sys.stderr.write("nothing to release\n")
        return NOTHING_TO_RELEASE
    except release_notes.NotesError as error:
        sys.stderr.write(f"next_version: {error}\n")
        return FAILED
    sys.stdout.write(f"{_text(version)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
