"""The settings of one instance, read from config.toml (design §3.2).

The numbers of the design are the defaults below, and the file overrides them key by key. A key
that nothing reads is an error, so a misspelled key cannot silently keep its default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

CONFIG_FILE_NAME: Final = "config.toml"
TOKEN_FILE_DEFAULT: Final = "secrets/telegram.token"  # noqa: S105 -- a file name, not a secret

LIVENESS_INTERVAL_S: Final = 60
STOP_GRACE_MIN: Final = 2
LOST_COMMAND_MIN: Final = 2
LINK_UNREACHABLE_MIN: Final = 10

COMMAND_TIMEOUT_MIN: Final = 10
QUOTA_WAIT_MAX_H: Final = 24
RETRY_BUDGET_PER_DAY: Final = 1
RESTART_BUDGET_PER_HOUR: Final = 1
APPROVAL_EXPIRY_H: Final = 24

REMINDER_FIRST_H: Final = 4
REMINDER_REPEAT_H: Final = 24

REASONER_TIMEOUT_S: Final = 120
REASONER_COOLDOWN_MIN: Final = 5
REASONER_MAX_PARALLEL: Final = 2


class SettingsError(Exception):
    """A config.toml that cannot be used; the message names the key at fault."""


@dataclass(frozen=True)
class Component:
    name: str
    path: Path


@dataclass(frozen=True)
class TelegramSettings:
    token_file: Path
    owners: tuple[int, ...]


@dataclass(frozen=True)
class OperatorSettings:
    liveness_interval_s: int
    stop_grace_min: int
    lost_command_min: int
    link_unreachable_min: int


@dataclass(frozen=True)
class InfrastructureSettings:
    command_timeout_min: int
    quota_wait_max_h: int
    retry_budget_per_day: int
    restart_budget_per_hour: int
    approval_expiry_h: int


@dataclass(frozen=True)
class ProductHandlerSettings:
    reminder_first_h: int
    reminder_repeat_h: int


@dataclass(frozen=True)
class HandlersSettings:
    infrastructure: InfrastructureSettings
    product: ProductHandlerSettings


@dataclass(frozen=True)
class ReasonerSettings:
    ladder: tuple[str, ...]
    timeout_s: int
    cooldown_min: int
    max_parallel: int


@dataclass(frozen=True)
class Settings:
    product: str
    components: tuple[Component, ...]
    telegram: TelegramSettings
    operator: OperatorSettings
    handlers: HandlersSettings
    reasoner: ReasonerSettings | None


def load_settings(instance_dir: Path) -> Settings:
    """Read the config.toml of an instance directory; a relative token_file is taken from it."""
    config = instance_dir / CONFIG_FILE_NAME
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        message = f"no {CONFIG_FILE_NAME} in {instance_dir}"
        raise SettingsError(message) from error
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = f"{config} is not valid TOML: {error}"
        raise SettingsError(message) from error
    return _parse(data, instance_dir)


def _parse(data: Mapping[str, object], instance_dir: Path) -> Settings:
    root = _Table(data, "")
    settings = Settings(
        product=root.text("product"),
        components=_components(root),
        telegram=_telegram(root.table("channels").table("telegram"), instance_dir),
        operator=_operator(root.table("operator", {})),
        handlers=_handlers(root.table("handlers", {})),
        reasoner=_reasoner(root.table("reasoner")) if root.has("reasoner") else None,
    )
    root.reject_unknown()
    return settings


def _components(root: _Table) -> tuple[Component, ...]:
    components = tuple(_component(table) for table in root.tables("components"))
    if not components:
        raise SettingsError(_problem("components", "must list at least one component"))
    names = [component.name for component in components]
    for index, name in enumerate(names):
        if name in names[:index]:
            key = f"components[{index}].name"
            raise SettingsError(_problem(key, f"repeats the component name '{name}'"))
    return components


def _component(table: _Table) -> Component:
    if table.has("host"):
        problem = "is not supported yet; a component runs on this machine"
        raise SettingsError(_problem(table.qualified("host"), problem))
    name = table.text("name")
    path = Path(table.text("path")).expanduser()
    if not path.is_absolute():
        problem = "must be an absolute path or start with ~"
        raise SettingsError(_problem(table.qualified("path"), problem))
    return Component(name=name, path=path)


def _telegram(table: _Table, instance_dir: Path) -> TelegramSettings:
    token_file = table.text("token_file", TOKEN_FILE_DEFAULT)
    return TelegramSettings(token_file=instance_dir / token_file, owners=table.integers("owners"))


def _operator(table: _Table) -> OperatorSettings:
    return OperatorSettings(
        liveness_interval_s=table.integer("liveness_interval_s", LIVENESS_INTERVAL_S),
        stop_grace_min=table.integer("stop_grace_min", STOP_GRACE_MIN),
        lost_command_min=table.integer("lost_command_min", LOST_COMMAND_MIN),
        link_unreachable_min=table.integer("link_unreachable_min", LINK_UNREACHABLE_MIN),
    )


def _handlers(table: _Table) -> HandlersSettings:
    return HandlersSettings(
        infrastructure=_infrastructure(table.table("infrastructure", {})),
        product=_product_handler(table.table("product", {})),
    )


def _infrastructure(table: _Table) -> InfrastructureSettings:
    return InfrastructureSettings(
        command_timeout_min=table.integer("command_timeout_min", COMMAND_TIMEOUT_MIN),
        quota_wait_max_h=table.integer("quota_wait_max_h", QUOTA_WAIT_MAX_H),
        retry_budget_per_day=table.integer("retry_budget_per_day", RETRY_BUDGET_PER_DAY),
        restart_budget_per_hour=table.integer("restart_budget_per_hour", RESTART_BUDGET_PER_HOUR),
        approval_expiry_h=table.integer("approval_expiry_h", APPROVAL_EXPIRY_H),
    )


def _product_handler(table: _Table) -> ProductHandlerSettings:
    return ProductHandlerSettings(
        reminder_first_h=table.integer("reminder_first_h", REMINDER_FIRST_H),
        reminder_repeat_h=table.integer("reminder_repeat_h", REMINDER_REPEAT_H),
    )


def _reasoner(table: _Table) -> ReasonerSettings:
    return ReasonerSettings(
        ladder=table.strings("ladder", ()),
        timeout_s=table.integer("timeout_s", REASONER_TIMEOUT_S),
        cooldown_min=table.integer("cooldown_min", REASONER_COOLDOWN_MIN),
        max_parallel=table.integer("max_parallel", REASONER_MAX_PARALLEL),
    )


def _problem(key: str, problem: str) -> str:
    return f"'{key}' {problem}"


class _Table:
    """A TOML table that records the keys it reads, so a key nothing reads is reported as unknown.

    A default of None makes a key required: TOML has no null, so None never stands for a value
    that is present.
    """

    def __init__(self, data: Mapping[str, object], path: str) -> None:
        self._data = data
        self._path = path
        self._read: set[str] = set()
        self._children: list[_Table] = []

    def qualified(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def has(self, key: str) -> bool:
        return key in self._data

    def text(self, key: str, default: str | None = None) -> str:
        return self._scalar(key, str, default, "a string")

    def integer(self, key: str, default: int | None = None) -> int:
        return self._scalar(key, int, default, "an integer")

    def strings(self, key: str, default: tuple[str, ...] | None = None) -> tuple[str, ...]:
        return self._array(key, str, default, "strings")

    def integers(self, key: str, default: tuple[int, ...] | None = None) -> tuple[int, ...]:
        return self._array(key, int, default, "integers")

    def table(self, key: str, default: Mapping[str, object] | None = None) -> _Table:
        value = self._raw(key)
        if value is None:
            return self._child(self._fallback(key, default), key)
        if not isinstance(value, dict):
            raise SettingsError(_problem(self.qualified(key), "must be a table"))
        return self._child(cast("Mapping[str, object]", value), key)

    def tables(self, key: str) -> list[_Table]:
        items = self._list(key)
        if items is None:
            raise SettingsError(_problem(self.qualified(key), "is required"))
        tables: list[_Table] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                entry = f"{key}[{index}]"
                raise SettingsError(_problem(self.qualified(entry), "must be a table"))
            tables.append(self._child(cast("Mapping[str, object]", item), f"{key}[{index}]"))
        return tables

    def reject_unknown(self) -> None:
        for key in self._data:
            if key not in self._read:
                raise SettingsError(_problem(self.qualified(key), "is not a known key"))
        for child in self._children:
            child.reject_unknown()

    def _raw(self, key: str) -> object | None:
        self._read.add(key)
        return self._data.get(key)

    def _list(self, key: str) -> list[object] | None:
        value = self._raw(key)
        if value is None:
            return None
        if not isinstance(value, list):
            raise SettingsError(_problem(self.qualified(key), "must be an array"))
        return cast("list[object]", value)

    def _scalar[T](self, key: str, kind: type[T], default: T | None, expected: str) -> T:
        value = self._raw(key)
        if value is None:
            return self._fallback(key, default)
        if isinstance(value, bool) or not isinstance(value, kind):
            raise SettingsError(_problem(self.qualified(key), f"must be {expected}"))
        return value

    def _array[T](
        self, key: str, kind: type[T], default: tuple[T, ...] | None, expected: str
    ) -> tuple[T, ...]:
        items = self._list(key)
        if items is None:
            return self._fallback(key, default)
        typed = tuple(
            item for item in items if isinstance(item, kind) and not isinstance(item, bool)
        )
        if len(typed) != len(items):
            problem = f"must be an array of {expected}"
            raise SettingsError(_problem(self.qualified(key), problem))
        return typed

    def _fallback[T](self, key: str, default: T | None) -> T:
        if default is None:
            raise SettingsError(_problem(self.qualified(key), "is required"))
        return default

    def _child(self, data: Mapping[str, object], key: str) -> _Table:
        child = _Table(data, self.qualified(key))
        self._children.append(child)
        return child
