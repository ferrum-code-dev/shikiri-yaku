"""The names and recipes of an install (design §6.3): the only argument lists that may run.

The Reasoner names a tool, a manager and a package. Pydantic accepts them only inside a strict
pattern, and code builds the argument list from a fixed row of the recipe table, so no model text
becomes a word of a command. The table is also the allow-list: a manager without a row is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import StringConstraints, TypeAdapter, ValidationError

NAME_MAX_LENGTH: Final = 100
_NAME_CHARS: Final = "A-Za-z0-9._+"
_NAME: Final = rf"[{_NAME_CHARS}][{_NAME_CHARS}-]*"
_PLAIN_PATTERN: Final = rf"^{_NAME}$"
# keep: npm reads `user/repo` as a GitHub repository, so a slash is allowed only inside a scope
_SCOPED_PATTERN: Final = rf"^(?:{_NAME}|@{_NAME}/{_NAME})$"
_PLAIN: Final[TypeAdapter[str]] = TypeAdapter(
    Annotated[str, StringConstraints(pattern=_PLAIN_PATTERN, max_length=NAME_MAX_LENGTH)]
)
_SCOPED: Final[TypeAdapter[str]] = TypeAdapter(
    Annotated[str, StringConstraints(pattern=_SCOPED_PATTERN, max_length=NAME_MAX_LENGTH)]
)


@dataclass(frozen=True, slots=True)
class Recipe:
    """The fixed words that install with one manager; the package name is appended last.

    `scoped` lets the package name carry `@` and `/`, as npm's scoped package names do.
    """

    words: tuple[str, ...]
    scoped: bool = False


RECIPES: Final = MappingProxyType(
    {
        "brew": Recipe(("brew", "install")),
        "uv": Recipe(("uv", "tool", "install")),
        "npm": Recipe(("npm", "install", "-g"), scoped=True),
        "pipx": Recipe(("pipx", "install")),
        "cargo": Recipe(("cargo", "install")),
    }
)


class RefusalReason(StrEnum):
    BAD_TOOL = "bad_tool"
    BAD_PACKAGE = "bad_package"
    MANAGER_NOT_ALLOWED = "manager_not_allowed"
    MANAGER_MISSING = "manager_missing"


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why an install is not planned; the reason is what the caller acts on and forwards."""

    reason: RefusalReason


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """What an install will run; `argv` is the stored argument list, the only thing that runs."""

    tool: str
    manager: str
    package: str
    argv: tuple[str, ...]


def is_tool_name(name: str) -> bool:
    """Return whether `name` is a valid executable name: no path, scope or space in it."""
    return _is_valid(_PLAIN, name)


def _is_valid(adapter: TypeAdapter[str], value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def validate_install(
    tool: str, manager: str, package: str, host_managers: frozenset[str]
) -> InstallPlan | Refusal:
    """Return the plan that installs `package` with `manager` as `tool`, or why it is refused.

    `host_managers` is the set of managers found on this host (design §6.3, step 1).
    """
    if not is_tool_name(tool):
        return Refusal(RefusalReason.BAD_TOOL)
    recipe = RECIPES.get(manager)
    if recipe is None:
        return Refusal(RefusalReason.MANAGER_NOT_ALLOWED)
    if not _is_valid(_SCOPED if recipe.scoped else _PLAIN, package):
        return Refusal(RefusalReason.BAD_PACKAGE)
    if manager not in host_managers:
        return Refusal(RefusalReason.MANAGER_MISSING)
    return InstallPlan(tool, manager, package, (*recipe.words, package))
