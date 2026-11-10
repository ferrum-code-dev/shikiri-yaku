"""The settings of one instance and the host-facing CLI.

Each numeric setting has a default below. Instance settings can be overridden in config.toml, where
a key that nothing reads is an error. CLI settings use defaults without reading instance config.
"""

from __future__ import annotations

import re
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
    StrictBool,
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
SHIKIRU_EXECUTABLE: Final = "shikiru"
# keep: the Shikiru range the Operator was checked against; doctor fails outside it.
SHIKIRU_FIRST_SUPPORTED: Final = "0.1.0"
SHIKIRU_FIRST_UNSUPPORTED: Final = "0.3.0"

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
COMMAND_NAME_MAX_CHARS: Final = 32

COMMAND_TIMEOUT_MIN: Final = 10
QUOTA_WAIT_MAX_H: Final = 24
QUOTA_WAIT_MARGIN_MIN: Final = 1
RETRY_BUDGET_PER_DAY: Final = 1
RESTART_BUDGET_PER_HOUR: Final = 1
RESTART_CONFIRM_S: Final = 60
RESTART_POLL_S: Final = 5
RESTART_TIMEOUT_S: Final = 30
APPROVAL_EXPIRY_H: Final = 24
HOST_LOCK_POLL_S: Final = 1
INSTALL_OUTPUT_TAIL_LINES: Final = 20
JOURNAL_KEEP_BACKUPS: Final = 3

REMINDER_FIRST_H: Final = 4
REMINDER_REPEAT_H: Final = 24
MESSAGE_TITLE_MAX_CHARS: Final = 80
BUTTON_LABEL_MAX_CHARS: Final = 30

REASONER_TIMEOUT_S: Final = 120
REASONER_COOLDOWN_MIN: Final = 5
REASONER_MAX_PARALLEL: Final = 2
REASONER_INPUT_MAX_CHARS: Final = 4000
REASONER_ADAPTERS: Final = frozenset({"claude", "codex"})
_RUNG_PARTS: Final = ("adapter", "model", "effort")
_MIN_RUNG_PARTS: Final = 2

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
    group_id: StrictInt | None = None
    allow_other_members: StrictBool = False
    shared_group: StrictBool = False

    @model_validator(mode="after")
    def _shared_group_has_a_group(self) -> Self:
        # keep: without a group there is no shared chat for another instance's bot to be in
        if self.shared_group and self.group_id is None:
            message = "'shared_group' requires 'group_id'"
            raise PydanticCustomError(_RULE, message)
        return self


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
    command_name_max_chars: PositiveInt = COMMAND_NAME_MAX_CHARS

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
    quota_wait_margin_min: PositiveInt = QUOTA_WAIT_MARGIN_MIN
    retry_budget_per_day: PositiveInt = RETRY_BUDGET_PER_DAY
    restart_budget_per_hour: PositiveInt = RESTART_BUDGET_PER_HOUR
    restart_confirm_s: PositiveInt = RESTART_CONFIRM_S
    restart_poll_s: PositiveInt = RESTART_POLL_S
    approval_expiry_h: PositiveInt = APPROVAL_EXPIRY_H
    host_lock_poll_s: PositiveInt = HOST_LOCK_POLL_S
    install_output_tail_lines: PositiveInt = INSTALL_OUTPUT_TAIL_LINES


class ProductHandlerSettings(_FrozenModel):
    reminder_first_h: PositiveInt = REMINDER_FIRST_H
    reminder_repeat_h: PositiveInt = REMINDER_REPEAT_H
    message_title_max_chars: PositiveInt = MESSAGE_TITLE_MAX_CHARS
    button_label_max_chars: PositiveInt = BUTTON_LABEL_MAX_CHARS


class HandlersSettings(_FrozenModel):
    infrastructure: InfrastructureSettings = Field(default_factory=InfrastructureSettings)
    product: ProductHandlerSettings = Field(default_factory=ProductHandlerSettings)


def _known_adapter(value: str) -> str:
    if value not in REASONER_ADAPTERS:
        message = "unknown adapter '{adapter}'; the adapters are {known}"
        known = ", ".join(sorted(REASONER_ADAPTERS))
        raise PydanticCustomError(_RULE, message, {"adapter": value, "known": known})
    return value


def _split_rung(value: object) -> object:
    if not isinstance(value, str):
        return value
    parts = value.split(":")
    if not _MIN_RUNG_PARTS <= len(parts) <= len(_RUNG_PARTS):
        message = "must be written adapter:model or adapter:model:effort"
        raise PydanticCustomError(_RULE, message)
    return dict(zip(_RUNG_PARTS, parts, strict=False))


class LadderRung(_FrozenModel):
    """One rung of the Reasoner's ladder, written `adapter:model` or `adapter:model:effort`.

    The effort is optional; an adapter whose CLI has no flag for it ignores it.
    """

    adapter: Annotated[str, AfterValidator(_known_adapter)]
    model: Annotated[str, Field(min_length=1)]
    effort: Annotated[str, Field(min_length=1)] | None = None


type _Rung = Annotated[LadderRung, BeforeValidator(_split_rung)]


class ReasonerSettings(_FrozenModel):
    ladder: Annotated[tuple[_Rung, ...], _AT_LEAST_ONE]
    timeout_s: PositiveInt = REASONER_TIMEOUT_S
    cooldown_min: PositiveInt = REASONER_COOLDOWN_MIN
    max_parallel: PositiveInt = REASONER_MAX_PARALLEL
    input_max_chars: PositiveInt = REASONER_INPUT_MAX_CHARS


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


class CliSettings(_FrozenModel):
    """Default-only settings for host-facing CLI operations."""

    restart_timeout_s: PositiveInt = RESTART_TIMEOUT_S


def product_names(home: Path) -> list[str]:
    """Return the products under home: the directories with a config.toml, by name."""
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if re.fullmatch(NAME_PATTERN, entry.name) is not None
        and (entry / CONFIG_FILE_NAME).is_file()
    ]


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
