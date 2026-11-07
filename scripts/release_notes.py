"""Prints the notes of one version from the changelog, for the GitHub release.

`uv run python scripts/release_notes.py <version> [--changelog CHANGELOG.md]` prints the body of the
`## [<version>]` section without its heading. It exits non-zero with a message when the section is
missing or empty. The release workflow passes the output to `gh release create`. The script uses the
standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_CHANGELOG: Final = "CHANGELOG.md"
SECTION_PREFIX: Final = "## "
FAILED: Final = 1


class NotesError(Exception):
    """The changelog has no notes for the requested version."""


def section_body(text: str, version: str) -> str:
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        message = f"no section [{version}] in the changelog"
        raise NotesError(message)
    body = _lines_until_next_section(lines[start + 1 :])
    notes = "\n".join(body).strip()
    if not notes:
        message = f"the section [{version}] of the changelog is empty"
        raise NotesError(message)
    return notes


def _lines_until_next_section(lines: Sequence[str]) -> list[str]:
    body: list[str] = []
    for line in lines:
        if line.startswith(SECTION_PREFIX):
            break
        body.append(line)
    return body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the changelog notes of one version.")
    parser.add_argument("version")
    parser.add_argument("--changelog", type=Path, default=Path(DEFAULT_CHANGELOG))
    args = parser.parse_args(argv)
    try:
        notes = section_body(args.changelog.read_text(encoding="utf-8"), args.version)
    except NotesError as error:
        sys.stderr.write(f"release_notes: {error}\n")
        return FAILED
    sys.stdout.write(f"{notes}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
