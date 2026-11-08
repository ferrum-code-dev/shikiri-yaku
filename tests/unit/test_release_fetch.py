"""Fetching a release's wheel through a fake release source: its assets, its checksum, and the
choice of the newest release."""

from __future__ import annotations

import hashlib
from typing import Final

import pytest

from shikiri_yaku.app.release_fetch import Wheel, fetch_wheel, newest
from shikiri_yaku.domain.releases import ReleaseError
from tests.support.releases import FakeReleaseSource, asset_url, release

WHEEL: Final = "shikiri_yaku-0.2.0-py3-none-any.whl"
WHEEL_BYTES: Final = b"the wheel"
DIGEST: Final = hashlib.sha256(WHEEL_BYTES).hexdigest()
SUMS: Final = f"{DIGEST}  {WHEEL}\n".encode()


def _source(names: tuple[str, ...], files: dict[str, bytes]) -> FakeReleaseSource:
    return FakeReleaseSource(
        listing=[release("v0.2.0", names=names)],
        files={asset_url("v0.2.0", name): data for name, data in files.items()},
    )


@pytest.mark.parametrize(
    ("names", "files", "cause"),
    [
        pytest.param(("SHA256SUMS",), {"SHA256SUMS": SUMS}, f"has no {WHEEL}", id="no-wheel"),
        pytest.param((WHEEL,), {WHEEL: WHEEL_BYTES}, "has no SHA256SUMS", id="no-sums"),
        pytest.param(
            (WHEEL, "SHA256SUMS"),
            {WHEEL: b"damaged", "SHA256SUMS": SUMS},
            "does not match",
            id="checksum-mismatch",
        ),
    ],
)
def test_a_release_that_cannot_be_checked_installs_nothing(
    names: tuple[str, ...], files: dict[str, bytes], cause: str
) -> None:
    source = _source(names, files)

    with pytest.raises(ReleaseError, match=cause):
        fetch_wheel(source, source.listing[0])


def test_a_missing_asset_is_refused_before_anything_is_downloaded() -> None:
    source = _source((WHEEL,), {WHEEL: WHEEL_BYTES})

    with pytest.raises(ReleaseError):
        fetch_wheel(source, source.listing[0])
    assert source.downloads == []


def test_a_matching_wheel_is_returned_with_its_name_and_bytes() -> None:
    source = _source((WHEEL, "SHA256SUMS"), {WHEEL: WHEEL_BYTES, "SHA256SUMS": SUMS})

    assert fetch_wheel(source, source.listing[0]) == Wheel(WHEEL, WHEEL_BYTES)


def test_no_usable_release_is_an_error_that_says_so() -> None:
    with pytest.raises(ReleaseError, match="no release is published"):
        newest(FakeReleaseSource(listing=[release("v0.2.0", draft=True)]))
