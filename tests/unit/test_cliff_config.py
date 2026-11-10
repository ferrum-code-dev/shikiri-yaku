from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final, cast

import pytest

CLIFF: Final = Path(__file__).resolve().parents[2] / "cliff.toml"


def _tag_pattern() -> str:
    config = cast("dict[str, dict[str, object]]", tomllib.loads(CLIFF.read_text(encoding="utf-8")))
    return str(config["git"]["tag_pattern"])


@pytest.mark.parametrize(
    ("tag", "is_release"),
    [
        ("v0.1.0", True),
        ("v12.0.3", True),
        ("v1.0.0-rc1", False),
        ("v0.1.0.post1", False),
        ("v0.1", False),
        ("0.1.0", False),
    ],
)
def test_the_tag_pattern_takes_final_release_tags_only(*, tag: str, is_release: bool) -> None:
    assert bool(re.search(_tag_pattern(), tag)) is is_release
