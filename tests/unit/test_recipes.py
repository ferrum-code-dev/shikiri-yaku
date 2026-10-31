"""The names, the recipes and the refusals of an install (design §6.3, principle 4)."""

from __future__ import annotations

import re
import shlex
from typing import Final

import pytest

from shikiri_yaku.domain.recipes import (
    NAME_MAX_LENGTH,
    RECIPES,
    InstallPlan,
    Refusal,
    RefusalReason,
    is_tool_name,
    validate_install,
)

ALL_MANAGERS: Final = frozenset(RECIPES)
BARE_WORD: Final = re.compile(r"-?[a-z]+")
HOSTILE_NAMES: Final = (
    "",
    "ff mpeg",
    "ffmpeg;echo",
    "$(echo)",
    "`echo`",
    "it's",
    'say "hi"',
    "ffmpeg\n",
    "ffmpeg\necho",
    "-ffmpeg",
    "a|b",
    "a&b",
    "a>b",
    "a/b",
    "@scope/ffmpeg",
    "é",
    "x" * (NAME_MAX_LENGTH + 1),
)


@pytest.mark.parametrize("name", ["ffmpeg", "python3.14", "my_tool", "g++", "a-b", "A", "x" * 100])
def test_a_valid_executable_name_passes(name: str) -> None:
    assert is_tool_name(name)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_or_malformed_executable_name_is_refused(name: str) -> None:
    assert not is_tool_name(name)


@pytest.mark.parametrize(
    ("manager", "package", "valid"),
    [
        ("npm", "ruff", True),
        ("npm", "@scope/pkg", True),
        ("brew", "@scope/pkg", False),
        ("npm", "a b", False),
        ("npm", "-pkg", False),
        ("npm", "x" * NAME_MAX_LENGTH, True),
        ("npm", "x" * (NAME_MAX_LENGTH + 1), False),
        ("brew", "x" * NAME_MAX_LENGTH, True),
    ],
)
def test_the_package_pattern_depends_on_the_manager(
    manager: str, package: str, *, valid: bool
) -> None:
    result = validate_install("ffmpeg", manager, package, ALL_MANAGERS)

    assert isinstance(result, InstallPlan) is valid


@pytest.mark.parametrize(
    ("tool", "manager", "package", "reason"),
    [
        ("ff mpeg", "brew", "ffmpeg", RefusalReason.BAD_TOOL),
        ("ffmpeg;echo", "brew", "ffmpeg", RefusalReason.BAD_TOOL),
        ("-ffmpeg", "brew", "ffmpeg", RefusalReason.BAD_TOOL),
        ("", "brew", "ffmpeg", RefusalReason.BAD_TOOL),
        ("ffmpeg", "apt", "ffmpeg", RefusalReason.MANAGER_NOT_ALLOWED),
        ("ffmpeg", "dnf", "ffmpeg", RefusalReason.MANAGER_NOT_ALLOWED),
        ("ffmpeg", "sudo", "ffmpeg", RefusalReason.MANAGER_NOT_ALLOWED),
        ("ffmpeg", "brew;echo", "ffmpeg", RefusalReason.MANAGER_NOT_ALLOWED),
        ("ffmpeg", "brew", "ff mpeg", RefusalReason.BAD_PACKAGE),
        ("ffmpeg", "brew", "-ffmpeg", RefusalReason.BAD_PACKAGE),
        ("ffmpeg", "brew", "$(echo)", RefusalReason.BAD_PACKAGE),
        ("ffmpeg", "brew", "ffmpeg\n", RefusalReason.BAD_PACKAGE),
        ("ffmpeg", "brew", "@scope/ffmpeg", RefusalReason.BAD_PACKAGE),
        ("ffmpeg", "brew", "x" * (NAME_MAX_LENGTH + 1), RefusalReason.BAD_PACKAGE),
    ],
)
def test_a_hostile_or_unknown_name_is_refused_with_its_reason(
    tool: str, manager: str, package: str, reason: RefusalReason
) -> None:
    assert validate_install(tool, manager, package, ALL_MANAGERS) == Refusal(reason)


def test_a_manager_missing_on_this_host_is_refused() -> None:
    result = validate_install("ffmpeg", "brew", "ffmpeg", frozenset({"uv"}))

    assert result == Refusal(RefusalReason.MANAGER_MISSING)


@pytest.mark.parametrize(
    ("manager", "package", "argv"),
    [
        ("brew", "ffmpeg", ("brew", "install", "ffmpeg")),
        ("uv", "ruff", ("uv", "tool", "install", "ruff")),
        ("npm", "@scope/pkg", ("npm", "install", "-g", "@scope/pkg")),
        ("pipx", "httpie", ("pipx", "install", "httpie")),
        ("cargo", "ripgrep", ("cargo", "install", "ripgrep")),
    ],
)
def test_each_recipe_yields_its_argument_list_of_the_design(
    manager: str, package: str, argv: tuple[str, ...]
) -> None:
    result = validate_install("ffmpeg", manager, package, ALL_MANAGERS)

    assert result == InstallPlan("ffmpeg", manager, package, argv)


@pytest.mark.parametrize("manager", list(RECIPES))
def test_no_recipe_word_is_a_shell_string(manager: str) -> None:
    words = RECIPES[manager].words

    assert all(BARE_WORD.fullmatch(word) for word in words)
    assert shlex.split(" ".join(words)) == list(words)
