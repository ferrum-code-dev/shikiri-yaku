"""Picks the release to install and checks a wheel against the release's SHA256SUMS.

Pure: the caller fetches the releases and the bytes; nothing here reads the network or the disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

SUMS_NAME: Final = "SHA256SUMS"
# keep: the tag form the release workflow publishes; a tag that does not match is not a release
_TAG: Final = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
_VERSION: Final = re.compile(
    r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:(?:a|b|rc)[0-9]+)?(?:\.dev[0-9]+)?(?:\+[0-9a-zA-Z.]+)?"
)
# keep: the line sha256sum writes, in text mode ("  ") or binary mode (" *")
_SUMS_LINE: Final = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")


class ReleaseError(Exception):
    """A release cannot be installed; the message names the cause and nothing was installed."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str


@dataclass(frozen=True)
class Release:
    tag: str
    draft: bool
    assets: tuple[Asset, ...]

    @property
    def version(self) -> str:
        return self.tag.removeprefix("v")


def wheel_name(version: str) -> str:
    return f"shikiri_yaku-{version}-py3-none-any.whl"


def newest_release(releases: Iterable[Release]) -> Release | None:
    """Return the release with the highest version number, or None when none is usable.

    Usable means a version tag and not a draft. A pre-release is usable.
    """
    usable = [
        (numbers, release)
        for release in releases
        if not release.draft and (numbers := _numbers(release.tag, _TAG)) is not None
    ]
    if not usable:
        return None
    return max(usable, key=lambda pair: pair[0])[1]


def is_newer(release: Release, installed: str) -> bool:
    """Whether the release is a higher version than the installed one.

    The installed version counts by its release segment, so a development build of 0.1.4 is 0.1.4.
    One with no release segment is treated as older, so an update is offered rather than skipped.
    """
    candidate = _numbers(release.tag, _TAG)
    current = release_numbers(installed)
    return candidate is not None and (current is None or candidate > current)


def release_numbers(version: str) -> tuple[int, int, int] | None:
    """Return the X.Y.Z release segment of a version, or None when it has none.

    A pre-release, development or local suffix is ignored: 0.1.4.dev3+gabc gives (0, 1, 4).
    """
    return _numbers(version, _VERSION)


def asset_named(release: Release, name: str) -> Asset:
    for asset in release.assets:
        if asset.name == name:
            return asset
    message = f"release {release.tag} has no {name}"
    raise ReleaseError(message)


def expected_digest(sums: bytes, name: str) -> str:
    """Return the SHA-256 that SHA256SUMS lists for a file, in lower case."""
    try:
        text = sums.decode("utf-8")
    except UnicodeDecodeError as error:
        message = f"{SUMS_NAME} is not text"
        raise ReleaseError(message) from error
    digests: list[str] = []
    for line in text.splitlines():
        match = _SUMS_LINE.fullmatch(line)
        if match is None:
            message = f"{SUMS_NAME} has a malformed line: {line!r}"
            raise ReleaseError(message)
        digest, listed = match.groups()
        if listed == name:
            digests.append(digest.lower())
    if not digests:
        message = f"{SUMS_NAME} has no entry for {name}"
        raise ReleaseError(message)
    if len(digests) > 1:
        message = f"{SUMS_NAME} has {len(digests)} entries for {name}"
        raise ReleaseError(message)
    return digests[0]


def check_digest(data: bytes, expected: str, name: str) -> None:
    """Raise unless the bytes hash to the expected SHA-256."""
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        message = (
            f"{name} does not match its {SUMS_NAME} entry (expected {expected}, got {actual}); "
            "the download is damaged or not the released file, so nothing is installed"
        )
        raise ReleaseError(message)


def _numbers(text: str, pattern: re.Pattern[str]) -> tuple[int, int, int] | None:
    match = pattern.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))
