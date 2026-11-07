"""Deploys the tagged release of this checkout to this host, or rolls back to the last good one.

`uv run poe deploy` runs from the checkout at the release tag. It refuses a dirty tree or a HEAD
that is not tagged with the version in pyproject.toml, runs the gate, builds the wheel into the
release store, copies each product's journal, installs the wheel, restarts every product and
verifies. A failure after the install reinstalls the release that `current` names. `--rollback`
(`uv run poe rollback`) does only that reinstall. The script uses the standard library only, so it
never imports the package it replaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

PROGRAM: Final = "shikiri-yaku"
DISTRIBUTION: Final = "shikiri_yaku"
HOME_DIRECTORY_NAME: Final = ".shikiri-yaku"
CONFIG_FILE_NAME: Final = "config.toml"
JOURNAL_FILE_NAME: Final = "journal.db"
BACKUP_DIR_NAME: Final = "backups"
BACKUP_GLOB: Final = "journal-schema*.db"
NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
STORE_NAME: Final = "releases"
CURRENT_NAME: Final = "current"
KEEP_WHEELS: Final = 5
KEEP_BACKUPS: Final = 3
TAIL_LINES: Final = 20
ATTEMPTS: Final = 6
PAUSE_S: Final = 10
FAILED: Final = 1

GIT_STATUS: Final = ("git", "status", "--porcelain")
GIT_TAGS_AT_HEAD: Final = ("git", "tag", "--points-at", "HEAD")
GATE: Final = ("uv", "run", "poe", "check")
RESTART: Final = (PROGRAM, "--json", "restart")
VERSION: Final = (PROGRAM, "--version")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


type Runner = Callable[[Sequence[str], Path], CommandResult]
type Pause = Callable[[float], None]


@dataclass(frozen=True)
class Layout:
    checkout: Path
    store: Path
    home: Path


@dataclass(frozen=True)
class Backup:
    product: str
    journal: Path
    copy: Path


class StepError(Exception):
    """A step failed before anything was installed, so there is nothing to roll back."""


def build_argv(store: Path) -> tuple[str, ...]:
    return ("uv", "build", "--wheel", "--out-dir", str(store))


def install_argv(wheel: Path) -> tuple[str, ...]:
    return ("uv", "tool", "install", "--reinstall", str(wheel))


def doctor_argv(product: str) -> tuple[str, ...]:
    return (PROGRAM, "--product", product, "doctor")


def command_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the environment for a command, without the checkout's virtualenv on PATH.

    `uv run` puts the checkout's `.venv/bin` first, so a bare `shikiri-yaku` would be the checkout's
    copy, not the installed release that the verification must check.
    """
    env = dict(environ)
    venv = environ.get("VIRTUAL_ENV")
    if venv:
        bin_dir = str(Path(venv) / "bin")
        entries = environ.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(entry for entry in entries if entry != bin_dir)
    return env


def run_command(argv: Sequence[str], cwd: Path) -> CommandResult:
    sys.stdout.write(f"$ {shlex.join(argv)}\n")
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=command_environment(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return CommandResult(FAILED, "", str(error))
    return CommandResult(done.returncode, done.stdout, done.stderr)


def default_layout(checkout: Path) -> Layout:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Layout(
        checkout,
        Path(data_home) / PROGRAM / STORE_NAME,
        Path.home() / HOME_DIRECTORY_NAME,
    )


def project_version(checkout: Path) -> str:
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def check_release(run: Runner, checkout: Path, version: str) -> None:
    status = run(GIT_STATUS, checkout)
    if status.returncode != 0:
        message = f"git status failed:\n{_output(status)}"
        raise StepError(message)
    if status.stdout.strip():
        message = f"the work tree is not clean; commit or discard these changes:\n{status.stdout}"
        raise StepError(message)
    tags = run(GIT_TAGS_AT_HEAD, checkout)
    if tags.returncode != 0:
        message = f"git tag failed:\n{_output(tags)}"
        raise StepError(message)
    release = f"v{version}"
    if release not in tags.stdout.split():
        found = " ".join(tags.stdout.split()) or "none"
        message = (
            f"HEAD is not tagged {release} (tags on HEAD: {found}); "
            f"tag the release commit with `git tag {release}`"
        )
        raise StepError(message)


def run_gate(run: Runner, checkout: Path) -> None:
    result = run(GATE, checkout)
    if result.returncode != 0:
        message = f"the gate failed:\n{_output(result)}"
        raise StepError(message)


def build_wheel(run: Runner, layout: Layout, version: str) -> Path:
    layout.store.mkdir(parents=True, exist_ok=True)
    result = run(build_argv(layout.store), layout.checkout)
    if result.returncode != 0:
        message = f"the build failed:\n{_output(result)}"
        raise StepError(message)
    wheels = list(layout.store.glob(f"{DISTRIBUTION}-{version}-*.whl"))
    if not wheels:
        message = f"the build wrote no {DISTRIBUTION} {version} wheel to {layout.store}"
        raise StepError(message)
    return max(wheels, key=_modified)


def products(home: Path) -> list[str]:
    """Return the products under home, found as settings.product_names finds them."""
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if re.fullmatch(NAME_PATTERN, entry.name) and (entry / CONFIG_FILE_NAME).is_file()
    ]


def backup_journals(home: Path) -> list[Backup]:
    backups: list[Backup] = []
    for product in products(home):
        journal = home / product / JOURNAL_FILE_NAME
        if not journal.is_file():
            continue
        folder = journal.parent / BACKUP_DIR_NAME
        folder.mkdir(exist_ok=True)
        copy = copy_journal(journal, folder)
        prune(list(folder.glob(BACKUP_GLOB)), KEEP_BACKUPS)
        backups.append(Backup(product, journal, copy))
    return backups


def existing_backups(home: Path) -> list[Backup]:
    found: list[Backup] = []
    for product in products(home):
        journal = home / product / JOURNAL_FILE_NAME
        folder = journal.parent / BACKUP_DIR_NAME
        found.extend(Backup(product, journal, copy) for copy in sorted(folder.glob(BACKUP_GLOB)))
    return found


def copy_journal(journal: Path, folder: Path) -> Path:
    """Copy a journal with SQLite's online backup, named by its schema version."""
    try:
        source = sqlite3.connect(f"{journal.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        message = f"cannot open {journal}: {error}"
        raise StepError(message) from error
    try:
        (version,) = source.execute("PRAGMA user_version").fetchone()
        copy = folder / f"journal-schema{version}.db"
        copy.unlink(missing_ok=True)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    except sqlite3.Error as error:
        message = f"cannot back up {journal}: {error}"
        raise StepError(message) from error
    finally:
        source.close()
    return copy


def install_and_verify(
    run: Runner, pause: Pause, checkout: Path, wheel: Path, version: str
) -> list[str]:
    result = run(install_argv(wheel), checkout)
    if result.returncode != 0:
        return [f"installing {wheel.name} failed:\n{_output(result)}"]
    return restart_and_verify(run, pause, checkout, version)


def restart_and_verify(run: Runner, pause: Pause, checkout: Path, version: str) -> list[str]:
    """Restart every product and check the installed release; return the problems found."""
    result = run(RESTART, checkout)
    try:
        entries = json.loads(result.stdout)
        names = [str(entry["product"]) for entry in entries]
        failed = [
            f"{entry['product']}: {entry['result']}" for entry in entries if not entry["passed"]
        ]
    except ValueError, KeyError, TypeError:
        return [f"restart printed no product list:\n{_output(result)}"]
    if failed:
        return failed
    return verify(run, pause, checkout, version, names)


def verify(
    run: Runner, pause: Pause, checkout: Path, version: str, names: Sequence[str]
) -> list[str]:
    """Check the installed version once, then each product's doctor, retrying a failing doctor.

    A wrong version means the wrong binary runs, and waiting does not change that, so the doctor
    runs once in that case.
    """
    problems: list[str] = []
    shown = run(VERSION, checkout)
    attempts = ATTEMPTS
    if shown.returncode != 0 or shown.stdout.split()[-1:] != [version]:
        problems.append(f"{PROGRAM} --version does not show {version}")
        attempts = 1
    for name in names:
        result = doctor_until_it_passes(run, pause, checkout, name, attempts)
        if result.returncode != 0:
            problems.append(
                f"doctor fails for {name}: run `{PROGRAM} --product {name} doctor`\n"
                f"{_output(result)}"
            )
    return problems


def doctor_until_it_passes(
    run: Runner, pause: Pause, checkout: Path, name: str, attempts: int
) -> CommandResult:
    """Run one product's doctor until it passes or `attempts` runs have failed; return the last."""
    result = run(doctor_argv(name), checkout)
    for _ in range(attempts - 1):
        if result.returncode == 0:
            return result
        pause(PAUSE_S)
        result = run(doctor_argv(name), checkout)
    return result


def roll_back(run: Runner, pause: Pause, layout: Layout) -> list[str]:
    name = read_current(layout.store)
    if name is None:
        return ["nothing to go back to: no release is recorded as current"]
    wheel = layout.store / name
    if not wheel.is_file():
        return [f"{name}, which current names, is missing from {layout.store}"]
    return install_and_verify(run, pause, layout.checkout, wheel, version_of(name))


def read_current(store: Path) -> str | None:
    path = store / CURRENT_NAME
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_current(store: Path, wheel: Path) -> None:
    temporary = store / f"{CURRENT_NAME}.tmp"
    temporary.write_text(f"{wheel.name}\n", encoding="utf-8")
    temporary.replace(store / CURRENT_NAME)


def prune_releases(store: Path, current: Path) -> None:
    """Keep the newest wheels and the one `current` names; the rollback target is never removed."""
    prune(list(store.glob("*.whl")), KEEP_WHEELS, protected=frozenset({current}))


def prune(paths: Sequence[Path], keep: int, protected: frozenset[Path] = frozenset()) -> None:
    for path in sorted(paths, key=_modified, reverse=True)[keep:]:
        if path not in protected:
            path.unlink(missing_ok=True)


def deploy(run: Runner, pause: Pause, layout: Layout) -> int:
    try:
        version = project_version(layout.checkout)
        check_release(run, layout.checkout, version)
        run_gate(run, layout.checkout)
        wheel = build_wheel(run, layout, version)
        backups = backup_journals(layout.home)
    except (StepError, OSError) as stop:
        return _warn(str(stop))
    failures = install_and_verify(run, pause, layout.checkout, wheel, version)
    if not failures:
        write_current(layout.store, wheel)
        prune_releases(layout.store, wheel)
        _say(f"deployed {wheel.name}")
        return 0
    _warn("\n".join(failures))
    problems = roll_back(run, pause, layout)
    if problems:
        _warn("rollback failed:\n" + "\n".join(problems))
        _warn("\n".join(restore_hint(backup) for backup in backups))
        return FAILED
    _warn(f"rolled back to {read_current(layout.store)}")
    return FAILED


def rollback_only(run: Runner, pause: Pause, layout: Layout) -> int:
    problems = roll_back(run, pause, layout)
    if problems:
        _warn("rollback failed:\n" + "\n".join(problems))
        _warn("\n".join(restore_hint(backup) for backup in existing_backups(layout.home)))
        return FAILED
    _say(f"rolled back to {read_current(layout.store)}")
    return 0


def restore_hint(backup: Backup) -> str:
    return (
        f"{backup.product}: if the old version stops with JournalTooNewError, stop its service, "
        f"copy {backup.copy} over {backup.journal}, delete {backup.journal}-wal and "
        f"{backup.journal}-shm if they exist, and start the service again. Changes made after "
        "the backup are lost."
    )


def version_of(wheel_name: str) -> str:
    parts = wheel_name.split("-")
    return parts[1] if len(parts) > 1 else ""


def _output(result: CommandResult) -> str:
    lines = (result.stdout + result.stderr).strip().splitlines()
    return "\n".join(lines[-TAIL_LINES:]) or f"exit status {result.returncode}"


def _modified(path: Path) -> float:
    return path.stat().st_mtime


def _say(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _warn(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    return FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the tagged release, or roll it back.")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="only reinstall the release that current names, and restart",
    )
    arguments = parser.parse_args(argv)
    layout = default_layout(Path(__file__).resolve().parents[1])
    if arguments.rollback:
        return rollback_only(run_command, time.sleep, layout)
    return deploy(run_command, time.sleep, layout)


if __name__ == "__main__":
    sys.exit(main())
