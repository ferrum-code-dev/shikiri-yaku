"""Unit tests for the instance settings (design §3.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from shikiri_yaku.settings import (
    Component,
    HandlersSettings,
    InfrastructureSettings,
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
    (_MINIMAL + "\n" + _API, ("'components[1].name'", "'api'")),
    (
        _HEADER + '[[components]]\nname = "api"\npath = "src/shop-api"\n' + _TELEGRAM,
        ("'components[0].path' must be an absolute path",),
    ),
    (
        _HEADER
        + '[[components]]\nname = "api"\npath = "/src/api"\nhost = "build-box"\n'
        + _TELEGRAM,
        ("'components[0].host'", "not supported yet"),
    ),
]


def _write(directory: Path, config: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(config, encoding="utf-8")
    return directory


def test_the_design_sample_loads_with_its_documented_values(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _SAMPLE))

    assert settings == Settings(
        product="shop",
        components=(
            Component(name="api", path=Path.home() / "src" / "shop-api"),
            Component(name="web", path=Path.home() / "src" / "shop-web"),
        ),
        telegram=TelegramSettings(
            token_file=tmp_path / "secrets" / "telegram.token", owners=(123456789,)
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
            ladder=("claude:<model>:<effort>", "codex:<model>:<effort>"),
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


@pytest.mark.parametrize(("config", "expected"), _REJECTED)
def test_a_broken_file_is_rejected_with_the_key_at_fault(
    tmp_path: Path, config: str, expected: tuple[str, ...]
) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(_write(tmp_path, config))

    for fragment in expected:
        assert fragment in str(raised.value)
