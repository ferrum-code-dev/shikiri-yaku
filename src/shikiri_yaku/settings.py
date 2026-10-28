"""The settings of one instance, read from config.toml (design §3.2).

The numbers of the design are the defaults below, and the file overrides them key by key. A key
that nothing reads is an error, so a misspelled key cannot silently keep its default.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sized
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from pydantic import ValidationInfo
    from pydantic_core import ErrorDetails

CONFIG_FILE_NAME: Final = "config.toml"
TOKEN_FILE_DEFAULT: Final = "secrets/telegram.token"  # noqa: S105 -- a file name, not a secret
NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"

EVENT_WAIT_S: Final = 30
LIVENESS_INTERVAL_S: Final = 60
SCHEDULER_INTERVAL_S: Final = 30
OUTBOX_INTERVAL_S: Final = 2
HANDLER_TIMEOUT_S: Final = 900
DISPATCH_INTERVAL_S: Final = 2
FORWARD_RETRY_S: Final = 60
STOP_GRACE_MIN: Final = 2
LOST_COMMAND_MIN: Final = 2
LINK_UNREACHABLE_MIN: Final = 10
LINK_BACKOFF_INITIAL_S: Final = 1
LINK_BACKOFF_MAX_S: Final = 60
LINK_CALL_TIMEOUT_S: Final = 30
LINK_START_TIMEOUT_S: Final = 120
TELEGRAM_CALL_TIMEOUT_S: Final = 30
TELEGRAM_POLL_WAIT_S: Final = 20
INBOUND_INTERVAL_S: Final = 1

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

_MISSING: Final = "missing"
_EXTRA: Final = "extra_forbidden"
_RULE: Final = "invalid_setting"

type Name = Annotated[str, Field(pattern=NAME_PATTERN)]
type PositiveInt = Annotated[StrictInt, Field(gt=0)]


class SettingsError(Exception):
    """A config.toml that cannot be used; the message names the key at fault."""


def _expand_home(value: object) -> object:
    return Path(value).expanduser() if isinstance(value, str) else value


def _require_absolute(path: Path) -> Path:
    if not path.is_absolute():
        message = "must be an absolute path or start with ~"
        raise PydanticCustomError(_RULE, message)
    return path


def _inside_instance(path: Path, info: ValidationInfo) -> Path:
    if info.context is None:
        return path
    return Path(info.context["instance_dir"]) / path


def _reject_host(_value: object) -> None:
    message = "not supported yet; a component runs on this machine"
    raise PydanticCustomError(_RULE, message)


def _require_entries(value: object) -> object:
    if isinstance(value, Sized) and len(value) == 0:
        message = "must have at least one entry"
        raise PydanticCustomError(_RULE, message)
    return value


_AT_LEAST_ONE: Final = BeforeValidator(_require_entries)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Component(_FrozenModel):
    name: Name
    path: Annotated[Path, BeforeValidator(_expand_home), AfterValidator(_require_absolute)]
    host: Annotated[None, BeforeValidator(_reject_host)] = None


class TelegramSettings(_FrozenModel):
    token_file: Annotated[Path, AfterValidator(_inside_instance)] = Field(
        default=Path(TOKEN_FILE_DEFAULT), validate_default=True
    )
    owners: Annotated[tuple[StrictInt, ...], _AT_LEAST_ONE]


class OperatorSettings(_FrozenModel):
    event_wait_s: PositiveInt = EVENT_WAIT_S
    liveness_interval_s: PositiveInt = LIVENESS_INTERVAL_S
    scheduler_interval_s: PositiveInt = SCHEDULER_INTERVAL_S
    outbox_interval_s: PositiveInt = OUTBOX_INTERVAL_S
    handler_timeout_s: PositiveInt = HANDLER_TIMEOUT_S
    dispatch_interval_s: PositiveInt = DISPATCH_INTERVAL_S
    forward_retry_s: PositiveInt = FORWARD_RETRY_S
    stop_grace_min: PositiveInt = STOP_GRACE_MIN
    lost_command_min: PositiveInt = LOST_COMMAND_MIN
    link_unreachable_min: PositiveInt = LINK_UNREACHABLE_MIN
    link_backoff_initial_s: PositiveInt = LINK_BACKOFF_INITIAL_S
    link_backoff_max_s: PositiveInt = LINK_BACKOFF_MAX_S
    link_call_timeout_s: PositiveInt = LINK_CALL_TIMEOUT_S
    link_start_timeout_s: PositiveInt = LINK_START_TIMEOUT_S
    telegram_call_timeout_s: PositiveInt = TELEGRAM_CALL_TIMEOUT_S
    telegram_poll_wait_s: PositiveInt = TELEGRAM_POLL_WAIT_S
    inbound_interval_s: PositiveInt = INBOUND_INTERVAL_S

    @model_validator(mode="after")
    def _call_outlasts_poll(self) -> Self:
        # keep: a long poll that outlasts the HTTP timeout would fail every time it finds nothing
        if self.telegram_call_timeout_s <= self.telegram_poll_wait_s:
            message = "'telegram_call_timeout_s' must be greater than 'telegram_poll_wait_s'"
            raise PydanticCustomError(_RULE, message)
        return self


class InfrastructureSettings(_FrozenModel):
    command_timeout_min: PositiveInt = COMMAND_TIMEOUT_MIN
    quota_wait_max_h: PositiveInt = QUOTA_WAIT_MAX_H
    retry_budget_per_day: PositiveInt = RETRY_BUDGET_PER_DAY
    restart_budget_per_hour: PositiveInt = RESTART_BUDGET_PER_HOUR
    approval_expiry_h: PositiveInt = APPROVAL_EXPIRY_H


class ProductHandlerSettings(_FrozenModel):
    reminder_first_h: PositiveInt = REMINDER_FIRST_H
    reminder_repeat_h: PositiveInt = REMINDER_REPEAT_H


class HandlersSettings(_FrozenModel):
    infrastructure: InfrastructureSettings = Field(default_factory=InfrastructureSettings)
    product: ProductHandlerSettings = Field(default_factory=ProductHandlerSettings)


class ReasonerSettings(_FrozenModel):
    ladder: Annotated[tuple[str, ...], _AT_LEAST_ONE]
    timeout_s: PositiveInt = REASONER_TIMEOUT_S
    cooldown_min: PositiveInt = REASONER_COOLDOWN_MIN
    max_parallel: PositiveInt = REASONER_MAX_PARALLEL


class ChannelsSettings(_FrozenModel):
    telegram: TelegramSettings


class Settings(_FrozenModel):
    """The whole config.toml; `telegram` is read from `[channels.telegram]`."""

    product: Name
    components: Annotated[tuple[Component, ...], _AT_LEAST_ONE]
    channels: ChannelsSettings
    operator: OperatorSettings = Field(default_factory=OperatorSettings)
    handlers: HandlersSettings = Field(default_factory=HandlersSettings)
    reasoner: ReasonerSettings | None = None

    @property
    def telegram(self) -> TelegramSettings:
        return self.channels.telegram

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        names = [component.name for component in self.components]
        for index, name in enumerate(names):
            if name in names[:index]:
                message = "'components[{index}].name' repeats the component name '{name}'"
                raise PydanticCustomError(_RULE, message, {"index": index, "name": name})
        return self


def load_settings(instance_dir: Path) -> Settings:
    """Read the config.toml of an instance directory; a relative token_file is taken from it.

    Only the file's own keys are read: `[channels]` is the one place for the Telegram table, so a
    `[telegram]` table at the top level is an unknown key, not an alternative spelling.
    """
    config = instance_dir / CONFIG_FILE_NAME
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        message = f"no {CONFIG_FILE_NAME} in {instance_dir}"
        raise SettingsError(message) from error
    except (OSError, UnicodeDecodeError) as error:
        message = f"cannot read {config}: {error}"
        raise SettingsError(message) from error
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = f"{config} is not valid TOML: {error}"
        raise SettingsError(message) from error
    try:
        return Settings.model_validate(data, context={"instance_dir": instance_dir})
    except ValidationError as error:
        problems = [_translate(problem) for problem in error.errors()]
        raise SettingsError("\n".join(problems)) from error


def _translate(problem: ErrorDetails) -> str:
    key = _key_path(problem["loc"])
    if not key:
        return problem["msg"]
    if problem["type"] == _MISSING:
        return _problem(key, "is required")
    if problem["type"] == _EXTRA:
        return _problem(key, "is not a known key")
    return _problem(key, f"is invalid: {problem['msg']}")


def _key_path(location: tuple[int | str, ...]) -> str:
    key = ""
    for part in location:
        if isinstance(part, int):
            key += f"[{part}]"
        else:
            key = f"{key}.{part}" if key else part
    return key


def _problem(key: str, problem: str) -> str:
    return f"'{key}' {problem}"
