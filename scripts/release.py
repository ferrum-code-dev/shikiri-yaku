"""Cuts a release from this checkout: bumps the version, commits it, pushes main and tags it.

`uv run poe release patch|minor|major|<x.y.z> [--dry-run]` refuses unless the branch is main, the
work tree is clean, HEAD equals origin/main, the new version is newer than the current one and its
tag does not exist. It then rewrites the version in pyproject.toml, in the project's own entry of
uv.lock and in every whole-token occurrence of the old version in README.md, commits
`chore: release <version>`, pushes main, tags `v<version>` and pushes the tag. It does not wait for
the release workflow, which waits for CI and publishes. `--dry-run` runs the checks and prints every
change and command, and commits, pushes and tags nothing. Commands go through an injected runner, so
the tests run no git. The script uses the standard library only.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

REMOTE: Final = "origin"
MAIN: Final = "main"
TAG_PREFIX: Final = "v"
PROJECT_FILE: Final = "pyproject.toml"
LOCK_FILE: Final = "uv.lock"
README_FILE: Final = "README.md"
PROJECT_NAME: Final = "shikiri-yaku"
REPOSITORY: Final = "ferrum-code-dev/shikiri-yaku"
RUNS_URL: Final = f"https://github.com/{REPOSITORY}/actions/workflows/release.yml"
RELEASE_SUBJECT: Final = "chore: release {version}"
CO_AUTHOR: Final = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
PATCH: Final = "patch"
MINOR: Final = "minor"
MAJOR: Final = "major"
BUMPS: Final = frozenset({PATCH, MINOR, MAJOR})
VERSION_PATTERN: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAIL_LINES: Final = 20
FAILED: Final = 1

BRANCH_ARGV: Final = ("git", "branch", "--show-current")
STATUS_ARGV: Final = ("git", "status", "--porcelain")
FETCH_ARGV: Final = ("git", "fetch", "--tags", REMOTE)
HEAD_ARGV: Final = ("git", "rev-parse", "HEAD")
ORIGIN_MAIN_ARGV: Final = ("git", "rev-parse", f"{REMOTE}/{MAIN}")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ReleaseError(Exception):
    """The release is refused, or stopped before it finished; the message names the reason."""


type Version = tuple[int, int, int]
type Runner = Callable[[Sequence[str], Path], CommandResult]


def parse_version(text: str) -> Version | None:
    match = VERSION_PATTERN.match(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def bumped(current: Version, part: str) -> Version:
    major, minor, patch = current
    if part == MAJOR:
        return (major + 1, 0, 0)
    if part == MINOR:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def next_version(current: Version, request: str) -> Version:
    """Return the version a request names: a bump of the current one, or a newer explicit one."""
    if request in BUMPS:
        return bumped(current, request)
    explicit = parse_version(request)
    if explicit is None:
        message = f"{request!r} is not {PATCH}, {MINOR}, {MAJOR} or an x.y.z version"
        raise ReleaseError(message)
    if explicit <= current:
        message = f"{format_version(explicit)} is not newer than {format_version(current)}"
        raise ReleaseError(message)
    return explicit


def _replace(text: str, pattern: str, replacement: str, filename: str, flags: int = 0) -> str:
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count == 0:
        message = f"{filename} does not hold the current version; change it by hand"
        raise ReleaseError(message)
    return new_text


def set_project_version(text: str, old: str, new: str) -> str:
    pattern = rf'^version = "{re.escape(old)}"$'
    return _replace(text, pattern, f'version = "{new}"', PROJECT_FILE, re.MULTILINE)


def set_lock_version(text: str, old: str, new: str) -> str:
    """Change the version line of the project's own package entry, and no other line."""
    pattern = rf'(^name = "{PROJECT_NAME}"\nversion = "){re.escape(old)}(")'
    return _replace(text, pattern, rf"\g<1>{new}\g<2>", LOCK_FILE, re.MULTILINE)


def set_readme_versions(text: str, old: str, new: str) -> str:
    """Change each whole-token occurrence; `10.0.1.9` is another version and stays."""
    return re.sub(rf"(?<![\d.]){re.escape(old)}(?!\d|\.\d)", new, text)


def _read(run: Runner, checkout: Path, argv: Sequence[str]) -> CommandResult:
    result = run(argv, checkout)
    if result.returncode != 0:
        message = f"{shlex.join(argv)} failed:\n{_output(result)}"
        raise ReleaseError(message)
    return result


def _act(run: Runner, checkout: Path, argv: Sequence[str], *, dry_run: bool) -> None:
    if dry_run:
        _say(f"would run: {shlex.join(argv)}")
        return
    result = run(argv, checkout)
    if result.returncode != 0:
        message = (
            f"{shlex.join(argv)} failed; the earlier steps may have run, so check `git log`, "
            f"`git tag` and {REMOTE} before you retry:\n{_output(result)}"
        )
        raise ReleaseError(message)


def check_release_point(run: Runner, checkout: Path) -> None:
    branch = _read(run, checkout, BRANCH_ARGV).stdout.strip()
    if branch != MAIN:
        message = f"releases are cut from {MAIN}, and this checkout is on {branch or 'no branch'}"
        raise ReleaseError(message)
    if _read(run, checkout, STATUS_ARGV).stdout.strip():
        message = "the work tree has changes; commit or discard them before the release"
        raise ReleaseError(message)
    _read(run, checkout, FETCH_ARGV)
    head = _read(run, checkout, HEAD_ARGV).stdout.strip()
    origin = _read(run, checkout, ORIGIN_MAIN_ARGV).stdout.strip()
    if head != origin:
        message = f"HEAD is not {REMOTE}/{MAIN}; push or pull {MAIN} until they are equal"
        raise ReleaseError(message)


def current_version_text(checkout: Path) -> str:
    project = tomllib.loads((checkout / PROJECT_FILE).read_text(encoding="utf-8"))
    return str(project.get("project", {}).get("version", ""))


def release(run: Runner, checkout: Path, request: str, *, dry_run: bool) -> int:
    try:
        new = _release(run, checkout, request, dry_run=dry_run)
    except (ReleaseError, OSError, ValueError) as error:
        return _warn(f"release refused: {error}")
    _say(f"release workflow runs: {RUNS_URL}")
    if dry_run:
        _say("dry run: nothing was committed, pushed or tagged")
    else:
        _say(f"released {new}")
    return 0


def _release(run: Runner, checkout: Path, request: str, *, dry_run: bool) -> str:
    check_release_point(run, checkout)
    old_text = current_version_text(checkout)
    current = parse_version(old_text)
    if current is None:
        message = f"{PROJECT_FILE} has no x.y.z version: {old_text!r}"
        raise ReleaseError(message)
    new_name = format_version(next_version(current, request))
    tag = f"{TAG_PREFIX}{new_name}"
    if _read(run, checkout, ("git", "tag", "--list", tag)).stdout.strip():
        message = f"the tag {tag} already exists"
        raise ReleaseError(message)
    _rewrite(checkout, old_text, new_name, dry_run=dry_run)
    commands = (
        ("git", "add", PROJECT_FILE, LOCK_FILE, README_FILE),
        ("git", "commit", "-m", RELEASE_SUBJECT.format(version=new_name), "-m", CO_AUTHOR),
        ("git", "push", REMOTE, MAIN),
        ("git", "tag", tag),
        ("git", "push", REMOTE, tag),
    )
    for argv in commands:
        _act(run, checkout, argv, dry_run=dry_run)
    return new_name


def _rewrite(checkout: Path, old: str, new: str, *, dry_run: bool) -> None:
    """Write the three version edits, or only say which files they would change."""
    edits = {
        PROJECT_FILE: set_project_version,
        LOCK_FILE: set_lock_version,
        README_FILE: set_readme_versions,
    }
    for name, edit in edits.items():
        path = checkout / name
        original = path.read_text(encoding="utf-8")
        updated = edit(original, old, new)
        if updated == original:
            continue
        if dry_run:
            _say(f"would change {name}: {old} -> {new}")
        else:
            path.write_text(updated, encoding="utf-8")
            _say(f"changed {name}: {old} -> {new}")


def run_command(argv: Sequence[str], cwd: Path) -> CommandResult:
    _say(f"$ {shlex.join(argv)}")
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        return CommandResult(FAILED, "", str(error))
    return CommandResult(done.returncode, done.stdout, done.stderr)


def _output(result: CommandResult) -> str:
    lines = (result.stdout + result.stderr).strip().splitlines()
    return "\n".join(lines[-TAIL_LINES:]) or f"exit status {result.returncode}"


def _say(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _warn(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    return FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut a release from main.")
    parser.add_argument("version", help=f"{PATCH}, {MINOR}, {MAJOR} or an x.y.z version")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the checks and print every change and command; change nothing",
    )
    arguments = parser.parse_args(argv)
    checkout = Path(__file__).resolve().parents[1]
    return release(run_command, checkout, arguments.version, dry_run=arguments.dry_run)


if __name__ == "__main__":
    sys.exit(main())
