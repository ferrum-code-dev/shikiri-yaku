"""The update steps: store the wheel, back up the journals, install it, roll back on failure."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final

from shikiri_yaku.update.backup import backup_journals, existing_backups, restore_hint
from shikiri_yaku.update.host import FAILED, Host, UpdateError
from shikiri_yaku.update.store import (
    prune_releases,
    read_current,
    store_wheel,
    write_current,
)
from shikiri_yaku.update.verify import install_and_verify

if TYPE_CHECKING:
    from pathlib import Path

NO_WHEEL: Final = "shikiri-yaku update needs --wheel <path> or --rollback"


def run_update(host: Host, wheel: Path | None, *, rollback: bool) -> int:
    """Install the given wheel, or with rollback reinstall current; return the exit code."""
    if rollback and wheel is not None:
        return _warn("--wheel and --rollback cannot be used together")
    if rollback:
        return rollback_only(host)
    if wheel is None:
        return _warn(NO_WHEEL)
    return install_wheel(host, wheel)


def install_wheel(host: Host, wheel: Path) -> int:
    try:
        stored = store_wheel(host.layout.store, wheel)
        backup_journals(host.layout.home, host.settings.keep_backups)
    except (UpdateError, OSError) as stop:
        return _warn(str(stop))
    failures = install_and_verify(host, stored)
    if not failures:
        write_current(host.layout.store, stored)
        prune_releases(host.layout.store, stored, host.settings.keep_wheels)
        _say(f"installed {stored.name}")
        return 0
    _warn("\n".join(failures))
    problems = roll_back(host)
    if problems:
        _warn("rollback failed:\n" + "\n".join(problems))
        _warn_restore(host)
        return FAILED
    _warn(f"rolled back to {read_current(host.layout.store)}")
    return FAILED


def rollback_only(host: Host) -> int:
    problems = roll_back(host)
    if problems:
        _warn("rollback failed:\n" + "\n".join(problems))
        _warn_restore(host)
        return FAILED
    _say(f"rolled back to {read_current(host.layout.store)}")
    return 0


def roll_back(host: Host) -> list[str]:
    """Reinstall the release that current names; return the problems, none when it verifies."""
    name = read_current(host.layout.store)
    if name is None:
        return ["nothing to go back to: no release is recorded as current"]
    wheel = host.layout.store / name
    if not wheel.is_file():
        return [f"{name}, which current names, is missing from {host.layout.store}"]
    return install_and_verify(host, wheel)


def _warn_restore(host: Host) -> None:
    backups = existing_backups(host.layout.home)
    if backups:
        _warn("\n".join(restore_hint(backup) for backup in backups))


def _say(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _warn(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    return FAILED
