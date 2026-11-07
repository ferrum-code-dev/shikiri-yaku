from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scripts import release_notes

if TYPE_CHECKING:
    from pathlib import Path

CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0] - 2027-01-10

### Added

- The newest release, the first section.

## [0.2.0] - 2026-12-01

### Changed

- The middle section.

## [0.1.0] - 2026-11-07

### Added

- A release with a sub-heading of its own.

## [0.1] - 2026-10-01

- A prefix of the 0.1.0 section, with its own notes.

## [0.0.8] - 2026-09-01

## [0.0.7] - 2026-08-01

- The last section.
"""

NOTES_BY_VERSION: list[tuple[str, str]] = [
    ("0.3.0", "### Added\n\n- The newest release, the first section."),
    ("0.2.0", "### Changed\n\n- The middle section."),
    ("0.1.0", "### Added\n\n- A release with a sub-heading of its own."),
    ("0.1", "- A prefix of the 0.1.0 section, with its own notes."),
    ("0.0.7", "- The last section."),
]

MISSING_OR_EMPTY: list[str] = ["0.9.0", "Unreleased", "0.0.8", "0.0"]


@pytest.mark.parametrize(("version", "expected"), NOTES_BY_VERSION)
def test_returns_the_body_of_the_version_section(version: str, expected: str) -> None:
    assert release_notes.section_body(CHANGELOG, version) == expected


@pytest.mark.parametrize("version", MISSING_OR_EMPTY)
def test_refuses_a_missing_or_empty_section(version: str) -> None:
    with pytest.raises(release_notes.NotesError):
        release_notes.section_body(CHANGELOG, version)


def test_main_prints_the_notes_and_fails_on_a_missing_version(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")

    assert release_notes.main(["0.2.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out == "### Changed\n\n- The middle section.\n"

    assert release_notes.main(["9.9.9", "--changelog", str(changelog)]) == release_notes.FAILED
    assert "no section [9.9.9]" in capsys.readouterr().err
