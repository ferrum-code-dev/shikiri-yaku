from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final, cast

import pytest

CLIFF: Final = Path(__file__).resolve().parents[2] / "cliff.toml"


def _git() -> dict[str, object]:
    config = cast("dict[str, dict[str, object]]", tomllib.loads(CLIFF.read_text(encoding="utf-8")))
    return config["git"]


def _tag_pattern() -> str:
    return str(_git()["tag_pattern"])


def _outcome(subject: str) -> str:
    """git-cliff gives each commit to the first parser whose message pattern matches."""
    for parser in cast("list[dict[str, object]]", _git()["commit_parsers"]):
        pattern = parser.get("message")
        if isinstance(pattern, str) and re.search(pattern, subject):
            return "skipped" if parser.get("skip") else str(parser["group"])
    raise AssertionError(subject)


@pytest.mark.parametrize(
    ("subject", "outcome"),
    [
        ("docs: explain the release", "skipped"),
        ("test(release): cover the ancestor check", "skipped"),
        ("chore(deps): bump uv", "skipped"),
        ("ci: pin the action", "skipped"),
        ("build: change the wheel", "skipped"),
        ("style: reflow the module", "skipped"),
        ("refactor: rename a helper", "skipped"),
        ("feat: add a command", "Features"),
        ("fix: close a race", "Fixes"),
        ("perf: cache the lookup", "Other changes"),
        ("docs!: drop the old install path", "Breaking changes"),
        ("refactor(cli)!: rename the verb", "Breaking changes"),
    ],
)
def test_docs_and_maintenance_commits_are_skipped_unless_breaking(
    *, subject: str, outcome: str
) -> None:
    assert _outcome(subject) == outcome


def test_the_skip_parser_sits_after_both_breaking_parsers_and_before_the_catch_all() -> None:
    parsers = cast("list[dict[str, object]]", _git()["commit_parsers"])
    breaking = [
        index for index, parser in enumerate(parsers) if parser.get("group") == "Breaking changes"
    ]
    skip = [index for index, parser in enumerate(parsers) if parser.get("skip") is True]
    catch_all = [index for index, parser in enumerate(parsers) if parser.get("message") == ".*"]

    assert len(breaking) == 2
    assert len(skip) == 1
    assert len(catch_all) == 1
    assert max(breaking) < skip[0] < catch_all[0]


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
