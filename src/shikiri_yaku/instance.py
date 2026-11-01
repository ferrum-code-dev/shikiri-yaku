"""The instance directory of one product and the lock that keeps one instance running (design §3.1).

The lock is an flock on the file, so the kernel releases it when the holder exits, even after a
crash.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Generator

HOME_DIRECTORY_NAME: Final = ".shikiri-yaku"
LOCK_FILE_NAME: Final = "instance.lock"
JOURNAL_FILE_NAME: Final = "journal.db"
OUTPUT_DIR_NAME: Final = "output"
HOST_LOCK_FILE_NAME: Final = "host.lock"


class InstanceLockError(Exception):
    """The instance lock cannot be taken; the message says why."""


class InstanceLockedError(InstanceLockError):
    """Another process holds the instance lock, so this instance is already running."""


def instance_dir(product: str, home: Path | None = None) -> Path:
    """Return <home>/<product>, where home defaults to ~/.shikiri-yaku."""
    root = Path.home() / HOME_DIRECTORY_NAME if home is None else home
    return root / product


@contextmanager
def instance_lock(directory: Path) -> Generator[None]:
    """Hold the instance lock of a directory until the block ends; a second holder is refused."""
    lock_path = directory / LOCK_FILE_NAME
    try:
        handle = lock_path.open("a", encoding="utf-8")
    except OSError as error:
        message = f"cannot open {lock_path}: {error}"
        raise InstanceLockError(message) from error
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            message = f"{lock_path} is held by another process; this instance is already running"
            raise InstanceLockedError(message) from error
        yield
