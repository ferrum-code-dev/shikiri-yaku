"""Unit tests for the instance settings."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from shikiri_yaku.settings import (
    ChannelsSettings,
    Component,
    HandlersSettings,
    InfrastructureSettings,
    LadderRung,
    OperatorSettings,
    ProductHandlerSettings,
    ReasonerSettings,
    Settings,
    SettingsError,
    TelegramSettings,
    load_settings,
)

_SAMPLE: Final = """
product = "shop"

[[components]]
name = "api"
path = "~/src/shop-api"

[[components]]
name = "web"
path = "~/src/shop-web"
# host = "build-box"

[channels.telegram]
token_file = "secrets/telegram.token"
owners = [123456789]

[reasoner]
ladder = ["claude:<model>:<effort>", "codex:<model>:<effort>"]
timeout_s = 120
cooldown_min = 5
max_parallel = 2

[operator]
liveness_interval_s = 60
stop_grace_min = 2
lost_command_min = 2
link_unreachable_min = 10

[handlers.infrastructure]
command_timeout_min = 10
quota_wait_max_h = 24
retry_budget_per_day = 1
restart_budget_per_hour = 1
approval_expiry_h = 24

[handlers.product]
reminder_first_h = 4
reminder_repeat_h = 24
"""

_HEADER: Final = 'product = "shop"\n'
_API: Final = '[[components]]\nname = "api"\npath = "/src/shop-api"\n'
_TELEGRAM: Final = "[channels.telegram]\nowners = [123456789]\n"
_MINIMAL: Final = f"{_HEADER}\n{_API}\n{_TELEGRAM}"

_LADDER: Final = 'ladder = ["claude:<model>:<effort>", "codex:<model>:<effort>"]'


def _edit(original: str, replacement: str) -> str:
    assert original in _SAMPLE
    return _SAMPLE.replace(original, replacement, 1)


_REJECTED: list[tuple[str, tuple[str, ...]]] = [
    (_MINIMAL.replace(_HEADER, ""), ("'product' is required",)),
    (_HEADER + _TELEGRAM, ("'components' is required",)),
    (_HEADER + _API, ("'channels' is required",)),
    (_HEADER + _API + "[channels.telegram]\n", ("'channels.telegram.owners' is required",)),
    (_MINIMAL + "\n[surprise]\nvalue = 1\n", ("'surprise' is not a known key",)),
    (
        _MINIMAL + "\n[operator]\nliveness_intervall_s = 30\n",
        ("'operator.liveness_intervall_s' is not a known key",),
    ),
    (_MINIMAL + "\n[telegram]\nowners = [123456789]\n", ("'telegram' is not a known key",)),
    (
        _MINIMAL + '\n[channels.email]\naddress = "po@example.invalid"\n',
        ("'channels.email' is not a known key",),
    ),
    (
        _HEADER + _API + "[channels]\nowners = [999]\n" + _TELEGRAM,
        ("'channels.owners' is not a known key",),
    ),
    (
        _HEADER + _API + "[channels.telegram]\nowners = [123456789]\ntoken = 1\n",
        ("'channels.telegram.token' is not a known key",),
    ),
    (_MINIMAL + "\n" + _API, ("'components[1].name'", "'api'")),
    (
        _HEADER + '[[components]]\nname = "api"\npath = "src/shop-api"\n' + _TELEGRAM,
        ("'components[0].path' is invalid: must be an absolute path",),
    ),
    (_edit(_LADDER + "\n", ""), ("'reasoner.ladder' is required",)),
    (
        _HEADER
        + '[[components]]\nname = "api"\npath = "/src/api"\nhost = "build-box"\n'
        + _TELEGRAM,
        ("'components[0].host'", "not supported yet"),
    ),
    (
        _MINIMAL.replace(_TELEGRAM, _TELEGRAM + "shared_group = true\n"),
        ("'shared_group' requires 'group_id'",),
    ),
]

_BAD_VALUES: list[tuple[str, str]] = [
    ("product", _edit('product = "shop"', 'product = ""')),
    ("product", _edit('product = "shop"', 'product = "sh/op"')),
    ("product", _edit('product = "shop"', 'product = "sh.op"')),
    ("product", _edit('product = "shop"', 'product = "sh:op"')),
    ("product", _edit('product = "shop"', 'product = "sh op"')),
    ("product", _edit('product = "shop"', 'product = "-shop"')),
    ("components[1].name", _edit('name = "web"', 'name = "we/b"')),
    ("channels.telegram.owners", _edit("owners = [123456789]", "owners = []")),
    ("reasoner.ladder", _edit(_LADDER, "ladder = []")),
    ("reasoner.ladder[0].adapter", _edit(_LADDER, 'ladder = ["gemini:haiku:xhigh"]')),
    ("reasoner.ladder[0].adapter", _edit(_LADDER, 'ladder = ["Claude:haiku"]')),
    ("reasoner.ladder[0]", _edit(_LADDER, 'ladder = ["claude"]')),
    ("reasoner.ladder[0]", _edit(_LADDER, 'ladder = ["claude:haiku:xhigh:extra"]')),
    ("reasoner.ladder[0].model", _edit(_LADDER, 'ladder = ["claude::xhigh"]')),
    ("reasoner.ladder[0].effort", _edit(_LADDER, 'ladder = ["claude:haiku:"]')),
    ("operator.liveness_interval_s", _edit("liveness_interval_s = 60", "liveness_interval_s = 0")),
    ("operator.stop_grace_min", _edit("stop_grace_min = 2", "stop_grace_min = -1")),
    ("operator.lost_command_min", _edit("lost_command_min = 2", "lost_command_min = 0")),
    (
        "operator.link_unreachable_min",
        _edit("link_unreachable_min = 10", "link_unreachable_min = 0"),
    ),
    (
        "handlers.infrastructure.command_timeout_min",
        _edit("command_timeout_min = 10", "command_timeout_min = 0"),
    ),
    (
        "handlers.infrastructure.quota_wait_max_h",
        _edit("quota_wait_max_h = 24", "quota_wait_max_h = 0"),
    ),
    (
        "handlers.infrastructure.retry_budget_per_day",
        _edit("retry_budget_per_day = 1", "retry_budget_per_day = 0"),
    ),
    (
        "handlers.infrastructure.restart_budget_per_hour",
        _edit("restart_budget_per_hour = 1", "restart_budget_per_hour = 0"),
    ),
    (
        "handlers.infrastructure.approval_expiry_h",
        _edit("approval_expiry_h = 24", "approval_expiry_h = 0"),
    ),
    ("handlers.product.reminder_first_h", _edit("reminder_first_h = 4", "reminder_first_h = 0")),
    (
        "handlers.product.reminder_repeat_h",
        _edit("reminder_repeat_h = 24", "reminder_repeat_h = 0"),
    ),
    ("reasoner.timeout_s", _edit("timeout_s = 120", "timeout_s = 0")),
    ("reasoner.timeout_s", _edit("timeout_s = 120", "timeout_s = 1.5")),
    ("reasoner.cooldown_min", _edit("cooldown_min = 5", "cooldown_min = 0")),
    ("reasoner.max_parallel", _edit("max_parallel = 2", "max_parallel = 0")),
]


def _write(directory: Path, config: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(config, encoding="utf-8")
    return directory


def test_the_full_sample_config_loads_with_the_values_it_sets(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _SAMPLE))

    assert settings == Settings(
        product="shop",
        components=(
            Component(name="api", path=Path.home() / "src" / "shop-api"),
            Component(name="web", path=Path.home() / "src" / "shop-web"),
        ),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                token_file=tmp_path / "secrets" / "telegram.token", owners=(123456789,)
            )
        ),
        operator=OperatorSettings(
            liveness_interval_s=60, stop_grace_min=2, lost_command_min=2, link_unreachable_min=10
        ),
        handlers=HandlersSettings(
            infrastructure=InfrastructureSettings(
                command_timeout_min=10,
                quota_wait_max_h=24,
                retry_budget_per_day=1,
                restart_budget_per_hour=1,
                approval_expiry_h=24,
            ),
            product=ProductHandlerSettings(reminder_first_h=4, reminder_repeat_h=24),
        ),
        reasoner=ReasonerSettings(
            ladder=(
                LadderRung(adapter="claude", model="<model>", effort="<effort>"),
                LadderRung(adapter="codex", model="<model>", effort="<effort>"),
            ),
            timeout_s=120,
            cooldown_min=5,
            max_parallel=2,
        ),
    )


def test_a_minimal_file_gets_the_same_defaults_as_the_sample(tmp_path: Path) -> None:
    minimal_dir = _write(tmp_path / "minimal", _MINIMAL)
    minimal = load_settings(minimal_dir)
    sample = load_settings(_write(tmp_path / "sample", _SAMPLE))

    assert (minimal.operator, minimal.handlers) == (sample.operator, sample.handlers)
    assert minimal.telegram.token_file == minimal_dir / "secrets" / "telegram.token"
    assert minimal.reasoner is None


def test_telegram_group_id_is_optional_and_accepts_a_forum_group_id(tmp_path: Path) -> None:
    config = _MINIMAL.replace(
        _TELEGRAM,
        "[channels.telegram]\ngroup_id = -1001234567890\nowners = [123456789]\n",
    )

    settings = load_settings(_write(tmp_path, config))

    assert settings.telegram.group_id == -1001234567890


@pytest.mark.parametrize(("config", "expected"), _REJECTED)
def test_a_broken_file_is_rejected_with_the_key_at_fault(
    tmp_path: Path, config: str, expected: tuple[str, ...]
) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(_write(tmp_path, config))

    for fragment in expected:
        assert fragment in str(raised.value)


@pytest.mark.parametrize(("key", "config"), _BAD_VALUES)
def test_a_value_outside_its_rule_is_rejected_with_its_key(
    tmp_path: Path, key: str, config: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(_write(tmp_path, config))

    assert f"'{key}'" in str(raised.value)


_RUNGS: Final[list[tuple[str, LadderRung]]] = [
    ("claude:haiku:xhigh", LadderRung(adapter="claude", model="haiku", effort="xhigh")),
    ("codex:gpt-5", LadderRung(adapter="codex", model="gpt-5")),
]


@pytest.mark.parametrize(("text", "rung"), _RUNGS)
def test_a_rung_is_adapter_model_and_an_optional_effort(
    tmp_path: Path, text: str, rung: LadderRung
) -> None:
    settings = load_settings(_write(tmp_path, _edit(_LADDER, f'ladder = ["{text}"]')))

    assert settings.reasoner is not None
    assert settings.reasoner.ladder == (rung,)


@pytest.mark.parametrize("name", ["shop", "Shop-2_b", "2fast"])
def test_a_name_of_letters_digits_hyphens_and_underscores_is_accepted(
    tmp_path: Path, name: str
) -> None:
    settings = load_settings(_write(tmp_path, _edit('product = "shop"', f'product = "{name}"')))

    assert settings.product == name


def test_a_long_poll_that_outlasts_the_call_timeout_is_rejected(tmp_path: Path) -> None:
    config = _edit(
        "[operator]", "[operator]\ntelegram_call_timeout_s = 20\ntelegram_poll_wait_s = 20"
    )
    with pytest.raises(SettingsError) as raised:
        load_settings(_write(tmp_path, config))

    assert "'telegram_poll_wait_s'" in str(raised.value)
