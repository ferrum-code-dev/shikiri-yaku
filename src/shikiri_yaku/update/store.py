"""The release store: the installed wheels, and the file `current` naming the last good one."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

from shikiri_yaku.update.host import DISTRIBUTION, PROGRAM, UpdateError

if TYPE_CHECKING:
    from collections.abc import Sequence

STORE_NAME: Final = "releases"
CURRENT_NAME: Final = "current"


def default_store() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / PROGRAM / STORE_NAME


def store_wheel(store: Path, wheel: Path) -> Path:
    """Copy a shikiri_yaku wheel into the store and return the copy."""
    if not wheel.name.startswith(f"{DISTRIBUTION}-") or not wheel.name.endswith(".whl"):
        message = f"{wheel} is not a {DISTRIBUTION} wheel"
        raise UpdateError(message)
    if not wheel.is_file():
        message = f"{wheel} does not exist"
        raise UpdateError(message)
    store.mkdir(parents=True, exist_ok=True)
    copy = store / wheel.name
    if wheel.resolve() != copy.resolve():
        shutil.copy2(wheel, copy)
    return copy


def version_of(wheel_name: str) -> str:
    parts = wheel_name.split("-")
    return parts[1] if len(parts) > 1 else ""


def read_current(store: Path) -> str | None:
    path = store / CURRENT_NAME
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_current(store: Path, wheel: Path) -> None:
    temporary = store / f"{CURRENT_NAME}.tmp"
    temporary.write_text(f"{wheel.name}\n", encoding="utf-8")
    temporary.replace(store / CURRENT_NAME)


def prune_releases(store: Path, current: Path, keep: int) -> None:
    """Keep the newest wheels and the one `current` names; the rollback target is never removed."""
    prune(list(store.glob("*.whl")), keep, protected=frozenset({current}))


def prune(paths: Sequence[Path], keep: int, protected: frozenset[Path] = frozenset()) -> None:
    for path in sorted(paths, key=_modified, reverse=True)[keep:]:
        if path not in protected:
            path.unlink(missing_ok=True)


def _modified(path: Path) -> float:
    return path.stat().st_mtime
