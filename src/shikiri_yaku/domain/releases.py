"""Reads the X.Y.Z release segment of a version, as the Shikiru minimum is compared.

Pure: nothing here reads the network or the disk.
"""

from __future__ import annotations

import re
from typing import Final

_VERSION: Final = re.compile(
    r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:(?:a|b|rc)[0-9]+)?(?:\.dev[0-9]+)?(?:\+[0-9a-zA-Z.]+)?"
)


def release_numbers(version: str) -> tuple[int, int, int] | None:
    """Return the X.Y.Z release segment of a version, or None when it has none.

    A pre-release, development or local suffix is ignored: 0.1.4.dev3+gabc gives (0, 1, 4).
    """
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))
