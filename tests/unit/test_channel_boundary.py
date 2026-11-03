"""The command line uses a channel check without importing a channel adapter."""

from __future__ import annotations

import builtins
import importlib
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from shikiri_yaku.ports import ChannelCheck, CheckLine

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Generator, Mapping, Sequence
    from types import ModuleType

    import pytest

    from shikiri_yaku.app.stats import Measures
    from shikiri_yaku.settings import Settings

# keep: the repair group is the only channel-specific CLI surface
_CLI_REPAIR_MENTIONS = (
    "telegram = typer.Typer(",
    'app.add_typer(telegram, name="telegram"',
    "@telegram.command",
)
_TELEGRAM_MODULE = re.compile(r"shikiri_yaku\.adapters\.telegram(?:\.|$)")


class _FakeChannelCheck(ChannelCheck):
    def __init__(self) -> None:
        self.calls = 0

    @override
    async def verify(self) -> None:
        self.calls += 1

    @override
    async def report(self) -> list[CheckLine]:
        return [CheckLine("fake channel", "reported by the fake channel")]


class _Composition:
    def __init__(
        self, check: ChannelCheck, finished: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self.channel_check = check
        self.link_probes: Mapping[str, Callable[[], Awaitable[None]]] = {}
        self.rung_probes: Mapping[str, Callable[[], Awaitable[None]]] = {}
        self.jobs: Sequence[Callable[[], Coroutine[Any, Any, None]]] = [finished]
        self.recover: Callable[[], None] = lambda: None

    def close(self) -> None:
        pass


@contextmanager
def _fresh_cli_without_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[ModuleType, list[str]]]:
    package_prefix = "shikiri_yaku"
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == package_prefix or name.startswith(f"{package_prefix}.")
    }
    for name in original_modules:
        del sys.modules[name]
    blocked: list[str] = []
    original_import = builtins.__import__

    def reject_telegram_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if _TELEGRAM_MODULE.match(name):
            blocked.append(name)
            message = f"CLI imported a Telegram adapter: {name}"
            raise AssertionError(message)
        return original_import(name, *args, **kwargs)

    try:
        with monkeypatch.context() as guard:
            guard.setattr(builtins, "__import__", reject_telegram_import)
            yield importlib.import_module("shikiri_yaku.cli"), blocked
    finally:
        for name in tuple(sys.modules):
            if name == package_prefix or name.startswith(f"{package_prefix}."):
                del sys.modules[name]
        sys.modules.update(original_modules)


def test_cli_and_core_layers_name_no_concrete_channel_outside_config_and_repair_group() -> None:
    root = Path(__file__).resolve().parents[2]
    cli_file = root / "src" / "shikiri_yaku" / "cli.py"
    files = [
        cli_file,
        *(
            path
            for layer in ("app", "domain", "extensions", "journal")
            for path in sorted((root / "src" / "shikiri_yaku" / layer).rglob("*.py"))
        ),
    ]
    violations: list[str] = []
    for path in files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "telegram" not in line.lower():
                continue
            if path == cli_file and any(allowed in line for allowed in _CLI_REPAIR_MENTIONS):
                continue
            violations.append(f"{path.relative_to(root)}:{line_number}: {line.strip()}")

    assert not violations, "\n".join(violations)


def test_run_and_doctor_use_fake_channel_check_without_importing_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "shop"
    component = tmp_path / "api"
    instance.mkdir()
    component.mkdir()
    secrets = instance / "secrets"
    secrets.mkdir()
    token = secrets / "telegram.token"
    token.write_text("", encoding="utf-8")
    token.chmod(0o600)
    (instance / "config.toml").write_text(
        'product = "shop"\n\n'
        '[[components]]\nname = "api"\npath = "'
        + str(component)
        + '"\n\n[channels.telegram]\nowners = [123456789]\n',
        encoding="utf-8",
    )
    with _fresh_cli_without_telegram(monkeypatch) as (cli_module, blocked):
        check = _FakeChannelCheck()

        async def finished() -> None:
            return None

        def compose(_settings: Settings, _directory: Path) -> _Composition:
            return _Composition(check, finished)

        def read_measures(_directory: Path) -> Measures:
            raise NotImplementedError

        def adopt_topic(_directory: Path, _component: str, _thread_id: int) -> str:
            return ""

        def forget_topic(_directory: Path, _component: str) -> str:
            return ""

        arguments = ["--product", "shop", "--home", str(tmp_path)]
        for command in ("doctor", "run"):
            assert (
                cli_module.main(
                    [*arguments, command], compose, read_measures, adopt_topic, forget_topic
                )
                == 0
            )
        assert "Fake channel · reported by the fake channel" in capsys.readouterr().out
        assert check.calls == 2
        assert not any(_TELEGRAM_MODULE.match(name) for name in sys.modules)
        assert blocked == []
