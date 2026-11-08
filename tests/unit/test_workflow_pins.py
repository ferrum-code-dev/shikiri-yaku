from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PINNED_USE: Final = re.compile(r"uses: \S+@[0-9a-f]{40} # v\d")


def _workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("name", _workflow_names())
def test_every_action_is_pinned_to_a_full_sha_with_its_version(name: str) -> None:
    lines = (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
    uses = [line.strip() for line in lines if "uses:" in line]

    assert uses
    assert all(PINNED_USE.search(line) for line in uses), uses
