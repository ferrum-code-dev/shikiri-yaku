"""Checks an installed release: its version, and each product's doctor, retried while it starts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from shikiri_yaku.update.host import PROGRAM, CommandResult, Host, output_tail
from shikiri_yaku.update.store import version_of

if TYPE_CHECKING:
    from pathlib import Path

RESTART: Final = (PROGRAM, "--json", "restart")
VERSION: Final = (PROGRAM, "--version")


def install_argv(wheel: Path) -> tuple[str, ...]:
    return ("uv", "tool", "install", "--reinstall", str(wheel))


def doctor_argv(product: str) -> tuple[str, ...]:
    return (PROGRAM, "--product", product, "doctor")


def install_and_verify(host: Host, wheel: Path) -> list[str]:
    """Install a wheel, restart every product and verify it; return the problems found."""
    result = host.run(install_argv(wheel))
    if result.returncode != 0:
        tail = output_tail(result, host.settings.output_tail_lines)
        return [f"installing {wheel.name} failed:\n{tail}"]
    return restart_and_verify(host, version_of(wheel.name))


def restart_and_verify(host: Host, version: str) -> list[str]:
    result = host.run(RESTART)
    try:
        entries = json.loads(result.stdout)
        names = [str(entry["product"]) for entry in entries]
        failed = [
            f"{entry['product']}: {entry['result']}" for entry in entries if not entry["passed"]
        ]
    except ValueError, KeyError, TypeError:
        tail = output_tail(result, host.settings.output_tail_lines)
        return [f"restart printed no product list:\n{tail}"]
    if failed:
        return failed
    return verify(host, version, names)


def verify(host: Host, version: str, names: list[str]) -> list[str]:
    """Check the installed version once, then each product's doctor, retrying a failing doctor.

    A wrong version means the wrong binary runs, and waiting does not change that, so the doctor
    runs once in that case.
    """
    problems: list[str] = []
    shown = host.run(VERSION)
    attempts = host.settings.verify_attempts
    if shown.returncode != 0 or shown.stdout.split()[-1:] != [version]:
        problems.append(f"{PROGRAM} --version does not show {version}")
        attempts = 1
    for name in names:
        result = doctor_until_it_passes(host, name, attempts)
        if result.returncode != 0:
            tail = output_tail(result, host.settings.output_tail_lines)
            problems.append(
                f"doctor fails for {name}: run `{PROGRAM} --product {name} doctor`\n{tail}"
            )
    return problems


def doctor_until_it_passes(host: Host, name: str, attempts: int) -> CommandResult:
    """Run one product's doctor until it passes or `attempts` runs have failed; return the last."""
    result = host.run(doctor_argv(name))
    for _ in range(attempts - 1):
        if result.returncode == 0:
            return result
        host.pause(host.settings.verify_pause_s)
        result = host.run(doctor_argv(name))
    return result
