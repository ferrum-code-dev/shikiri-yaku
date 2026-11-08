"""Finds the newest release and fetches its wheel, checked against SHA256SUMS, through the port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shikiri_yaku.domain.releases import (
    SUMS_NAME,
    ReleaseError,
    asset_named,
    check_digest,
    expected_digest,
    newest_release,
    wheel_name,
)

if TYPE_CHECKING:
    from shikiri_yaku.domain.releases import Release
    from shikiri_yaku.ports import ReleaseSource


@dataclass(frozen=True)
class Wheel:
    name: str
    data: bytes


def newest(source: ReleaseSource) -> Release:
    """Return the newest usable release; raise ReleaseError when the source has none."""
    release = newest_release(source.releases())
    if release is None:
        message = "no release is published: none is a version tag that is not a draft"
        raise ReleaseError(message)
    return release


def fetch_wheel(source: ReleaseSource, release: Release) -> Wheel:
    """Download a release's wheel and its SHA256SUMS, and return the wheel once it matches.

    Both assets are found before anything is downloaded, so a release without one of them fails
    without a download.
    """
    name = wheel_name(release.version)
    wheel = asset_named(release, name)
    sums = asset_named(release, SUMS_NAME)
    expected = expected_digest(source.download(sums.url), name)
    data = source.download(wheel.url)
    check_digest(data, expected, name)
    return Wheel(name, data)
