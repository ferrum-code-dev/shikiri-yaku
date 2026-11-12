"""The agent modules stay apart, and a new agent needs only its module and one registry line."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, override

import pytest

import shikiri_yaku.adapters.agents as agents_package
from shikiri_yaku import bootstrap, settings
from shikiri_yaku.adapters.agents.common import AgentAdapter, BlockerAnswer, CliRung
from shikiri_yaku.adapters.agents.registry import AGENTS
from shikiri_yaku.bootstrap import reasoner_rungs
from shikiri_yaku.domain.classification import Verdict
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.ports import CommandResult, CommandRunner
from shikiri_yaku.settings import ReasonerSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PACKAGE: Final = Path(agents_package.__file__).parent
PACKAGE_NAME: Final = agents_package.__name__
SHARED: Final = frozenset({"common", "registry"})
# keep: a new agent must not need these files edited, so they name no agent (the registry may)
NAMELESS_FILES: Final = (
    PACKAGE / "common.py",
    Path(bootstrap.__file__),
    Path(settings.__file__),
)
REGISTRY_FILE: Final = PACKAGE / "registry.py"
_NAMED_TOKENS: Final = frozenset({tokenize.NAME, tokenize.STRING, tokenize.COMMENT})
_ANSWER: Final = {
    "verdict": "other",
    "tool": None,
    "manager": None,
    "package": None,
    "why": "a fake agent answers",
    "suggestion": "nothing to install",
}


def _agent_modules() -> list[Path]:
    return sorted(path for path in PACKAGE.glob("*.py") if path.stem not in {"__init__", *SHARED})


def _absolute(node: ast.ImportFrom) -> str:
    # keep: a relative import names the agent too, so it resolves against the package first
    if node.level == 0:
        return node.module or ""
    package = PACKAGE_NAME.split(".")
    parts = [*package[: len(package) - (node.level - 1)]]
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


def _agents_imported(source: str) -> set[str]:
    found: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = _absolute(node)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    for name in names:
        if name.startswith(f"{PACKAGE_NAME}."):
            found.add(name.removeprefix(f"{PACKAGE_NAME}.").split(".")[0])
    return found


def _agent_references(source: str, *, registrations: frozenset[str] = frozenset()) -> list[str]:
    # keep: an identifier that contains an agent name is a reference, so `ClaudeRung` fails too
    found: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in _NAMED_TOKENS:
            continue
        if token.type == tokenize.NAME and token.string in registrations:
            continue
        lowered = token.string.lower()
        if any(agent in lowered for agent in AGENTS):
            found.append(token.string)
    return found


@pytest.mark.parametrize("path", _agent_modules(), ids=lambda path: path.stem)
def test_an_agent_module_imports_no_other_agent_module(path: Path) -> None:
    stray = _agents_imported(path.read_text(encoding="utf-8")) - (SHARED | {path.stem})
    assert not stray, f"{path.name} imports the agent(s) {sorted(stray)}"


@pytest.mark.parametrize(
    "source",
    [
        "from .codex import CodexRung",
        "from . import codex",
        "from ..agents.codex import CodexRung",
        "from shikiri_yaku.adapters.agents.codex import CodexRung",
        "from shikiri_yaku.adapters.agents import codex",
        "import shikiri_yaku.adapters.agents.codex",
    ],
)
def test_the_import_guard_sees_relative_and_absolute_agent_imports(source: str) -> None:
    assert _agents_imported(source) == {"codex"}


@pytest.mark.parametrize("path", NAMELESS_FILES, ids=lambda path: path.name)
def test_a_shared_file_names_no_agent(path: Path) -> None:
    assert _agent_references(path.read_text(encoding="utf-8")) == []


def test_the_registry_names_an_agent_only_by_its_module() -> None:
    modules = frozenset(path.stem for path in _agent_modules())
    source = REGISTRY_FILE.read_text(encoding="utf-8")
    assert _agent_references(source, registrations=modules) == []


@pytest.mark.parametrize(
    "source",
    [
        "class ClaudeRung: ...",
        "codex_factory = 1",
        "x = 'claude'",
        "# codex is here",
    ],
)
def test_the_name_guard_sees_names_inside_identifiers_strings_and_comments(source: str) -> None:
    assert _agent_references(source) != []


def test_the_registry_may_not_name_an_agent_inside_a_larger_identifier() -> None:
    modules = frozenset({"claude", "codex"})
    assert _agent_references("claude.AGENT", registrations=modules) == []
    assert _agent_references("ClaudeRung", registrations=modules) != []


def _no_expert(*_args: object, **_kwargs: object) -> NoReturn:
    msg = "the fake agent has no Expert rung"
    raise AssertionError(msg)


class _FakeRung(CliRung):
    _adapter = "fake"
    _lookups = "Look nothing up."

    @override
    def _argv(self, prompt: str, scratch: Path) -> list[str]:
        return ["fake-agent", prompt]

    @override
    def _answer(self, result: CommandResult, scratch: Path) -> tuple[BlockerAnswer, None]:
        return BlockerAnswer.model_validate_json(result.stdout), None


class _FakeAgentRunner(CommandRunner):
    def __init__(self) -> None:
        self.argv: list[str] = []

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.argv = list(argv)
        return CommandResult(returncode=0, stdout=json.dumps(_ANSWER), stderr="")


def test_a_registered_agent_runs_through_a_bootstrap_built_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(AGENTS, "fake", AgentAdapter("fake", _FakeRung, _no_expert))
    runner = _FakeAgentRunner()
    config = ReasonerSettings.model_validate({"ladder": ["fake:model"]})
    [rung] = reasoner_rungs(config, runner, tmp_path)
    signal = Signal(
        project="api",
        kind=Kind.ESCALATION,
        source_id="esc-1",
        reason="blocked",
        text="the build stops",
        escalation_id="esc-1",
    )

    assessment = asyncio.run(rung.assess_blocker(signal, ["OS: macOS"]))

    assert rung.name == "fake:model"
    assert runner.argv[0] == "fake-agent"
    assert assessment.verdict is Verdict.OTHER
