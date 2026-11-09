"""The release segment of a version, as the Shikiru range reads it: pure rules."""

from __future__ import annotations

import pytest

from shikiri_yaku.domain.releases import release_numbers


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
