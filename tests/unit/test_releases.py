"""Which release an update installs, and whether a wheel matches its SHA256SUMS: pure rules."""

from __future__ import annotations

import hashlib
from typing import Final

import pytest

from shikiri_yaku.domain.releases import (
    Release,
    ReleaseError,
    check_digest,
    expected_digest,
    is_newer,
    newest_release,
    release_numbers,
)
from tests.support.releases import release

WHEEL: Final = "shikiri_yaku-0.2.0-py3-none-any.whl"
WHEEL_BYTES: Final = b"the wheel"
DIGEST: Final = hashlib.sha256(WHEEL_BYTES).hexdigest()


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        pytest.param(
            [release("v0.1.9"), release("v0.1.10")], "v0.1.10", id="newest-by-number-not-text"
        ),
        pytest.param([release("v0.2.0", draft=True), release("v0.1.4")], "v0.1.4", id="drafts"),
        pytest.param([release("v0.2.0")], "v0.2.0", id="pre-release-is-usable"),
        pytest.param(
            [release("release-2"), release("0.3.0"), release("v0.2"), release("v0.1.4")],
            "v0.1.4",
            id="tags-that-are-not-versions",
        ),
        pytest.param([release("v0.2.0", draft=True)], None, id="only-a-draft"),
        pytest.param([], None, id="no-releases"),
    ],
)
def test_the_newest_usable_release_is_picked(releases: list[Release], expected: str | None) -> None:
    picked = newest_release(releases)

    assert (picked.tag if picked else None) == expected


@pytest.mark.parametrize(
    ("tag", "installed", "newer"),
    [
        pytest.param("v0.1.5", "0.1.4", True, id="patch-up"),
        pytest.param("v0.1.10", "0.1.9", True, id="by-number"),
        pytest.param("v0.1.4", "0.1.4", False, id="already-newest"),
        pytest.param("v0.1.4", "0.1.10", False, id="installed-is-newer"),
        pytest.param("v0.1.4", "unknown", True, id="unreadable-installed-is-older"),
        pytest.param("v0.1.4", "0.1.4.dev3+gabc", False, id="development-build-of-the-release"),
        pytest.param("v0.1.5", "0.1.4.dev3+gabc", True, id="development-build-is-older"),
    ],
)
def test_an_update_is_offered_only_for_a_higher_version(
    tag: str, installed: str, *, newer: bool
) -> None:
    assert is_newer(release(tag), installed) is newer


@pytest.mark.parametrize(
    ("version", "numbers"),
    [
        pytest.param("0.1.4", (0, 1, 4), id="release"),
        pytest.param("0.1.4.dev3+gabc", (0, 1, 4), id="development-build"),
        pytest.param("1.0.0rc1", (1, 0, 0), id="pre-release"),
        pytest.param("0.1", None, id="not-three-numbers"),
        pytest.param("shikiru is fine", None, id="text"),
    ],
)
def test_the_release_segment_of_a_version_is_its_three_numbers(
    version: str, numbers: tuple[int, int, int] | None
) -> None:
    assert release_numbers(version) == numbers


def test_the_digest_is_read_from_the_line_for_the_wheel() -> None:
    sums = f"{'a' * 64}  other.tar.gz\n{DIGEST.upper()} *{WHEEL}\n".encode()

    assert expected_digest(sums, WHEEL) == DIGEST


@pytest.mark.parametrize(
    ("sums", "cause"),
    [
        pytest.param(b"not a checksum line\n", "malformed line", id="malformed"),
        pytest.param(f"{DIGEST}  other.whl\n".encode(), "has no entry", id="missing-entry"),
        pytest.param(f"{DIGEST}  {WHEEL}\n".encode() * 2, "2 entries", id="duplicate-entry"),
        pytest.param(b"\xff\xfe", "is not text", id="not-text"),
    ],
)
def test_a_damaged_sums_file_is_an_error_that_names_the_cause(sums: bytes, cause: str) -> None:
    with pytest.raises(ReleaseError, match=cause):
        expected_digest(sums, WHEEL)


def test_a_wheel_that_matches_its_digest_passes() -> None:
    check_digest(WHEEL_BYTES, DIGEST, WHEEL)


def test_a_wheel_that_differs_from_its_digest_names_the_wheel() -> None:
    with pytest.raises(ReleaseError, match=rf"{WHEEL} does not match.*nothing is installed"):
        check_digest(b"damaged", DIGEST, WHEEL)
