"""Expert CLI boundaries and ladder handoffs, without real agents."""

from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.adapters.agents.claude import ClaudeExpert
from shikiri_yaku.adapters.agents.codex import CodexExpert
from shikiri_yaku.app.expert_ladder import LadderExpert
from shikiri_yaku.domain.expert import (
    ExpertAnswer,
    ExpertRequest,
    ExpertResult,
    ExpertSession,
    ExpertTurn,
)
from shikiri_yaku.ports import CommandResult, ExpertError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ANSWER: dict[str, object] = {
    "reply": "Ready",
    "title": "Idea",
    "tasks": [],
    "findings": "Useful facts",
}


class Runner:
    def __init__(self, provider: str, answer: dict[str, object] | None = None) -> None:
        self.provider = provider
        self.answer = ANSWER if answer is None else answer
        self.argv: list[str] = []
        self.stdin = ""

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        self.argv = list(argv)
        self.stdin = stdin or ""
        if self.provider == "claude":
            return CommandResult(
                0,
                json.dumps(
                    {"is_error": False, "session_id": "session", "structured_output": self.answer}
                ),
                "",
            )
        self._write_answer(argv)
        return CommandResult(0, json.dumps({"type": "thread.started", "thread_id": "session"}), "")

    def _write_answer(self, argv: Sequence[str]) -> None:
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(self.answer))


EXPERTS = {"claude": ClaudeExpert, "codex": CodexExpert}


@pytest.mark.parametrize(
    ("provider", "resume"), [("claude", False), ("claude", True), ("codex", False), ("codex", True)]
)
def test_fixed_cli_arguments(provider: str, resume: bool, tmp_path: Path) -> None:  # noqa: FBT001
    runner = Runner(provider)
    rung = EXPERTS[provider]("model", runner, effort="high", timeout_s=20)
    request = ExpertRequest(tmp_path, {"api": tmp_path / "api"}, "hello")
    result = asyncio.run(rung.turn(request, session_id="old" if resume else None, handoff=None))
    argv = runner.argv
    if provider == "claude":
        schema = argv[argv.index("--json-schema") + 1]
        expected = [
            "claude",
            "-p",
            "--model",
            "model",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--restricted",
            "--tools",
            "Read,Grep,Glob",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            argv[argv.index("--mcp-config") + 1],
            "--allowedTools",
            "mcp__expert_web__web_fetch",
            "--effort",
            "high",
        ]
        if resume:
            expected += ["--resume", "old"]
        expected += ["--add-dir", str(tmp_path / "api")]
    else:
        schema = argv[argv.index("--output-schema") + 1]
        answer_file = argv[argv.index("--output-last-message") + 1]
        expected = [
            "codex",
            "exec",
            "--json",
            "--ignore-user-config",
            "--disable",
            "apps",
            "-c",
            'web_search="disabled"',
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            next(a for a in argv if a.startswith("mcp_servers.expert_web=")),
            "--skip-git-repo-check",
            "-m",
            "model",
            "--output-schema",
            schema,
            "--output-last-message",
            answer_file,
            "-c",
            'model_reasoning_effort="high"',
        ]
        if resume:
            expected += ["resume", "old"]
    assert argv == expected
    if provider == "claude":
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        assert list(config["mcpServers"]) == ["expert_web"]
        server = config["mcpServers"]["expert_web"]
    else:
        config = tomllib.loads(next(a for a in argv if a.startswith("mcp_servers.expert_web=")))
        assert list(config["mcp_servers"]) == ["expert_web"]
        server = config["mcp_servers"]["expert_web"]
        assert server["default_tools_approval_mode"] == "approve"
    assert server["command"] == "shikiri-yaku"
    assert server["args"][0] == "_expert-web"
    assert "hello" in runner.stdin
    assert result.session.session_id == "session"


@pytest.mark.parametrize("session", [None, "old"])
@pytest.mark.parametrize(
    ("rung_effort", "conversation_effort", "expected"),
    [
        (None, None, None),
        ("low", None, "low"),
        ("low", "high", "high"),
        (None, 'high"\nweb_search="enabled', 'high"\nweb_search="enabled'),
    ],
)
def test_codex_effort_override(
    session: str | None,
    rung_effort: str | None,
    conversation_effort: str | None,
    expected: str | None,
    tmp_path: Path,
) -> None:
    runner = Runner("codex")
    rung = CodexExpert("model", runner, effort=rung_effort, timeout_s=20)
    request = ExpertRequest(tmp_path, {"api": tmp_path}, "hello", effort=conversation_effort)
    asyncio.run(rung.turn(request, session_id=session, handoff=None))
    overrides = [arg for arg in runner.argv if arg.startswith("model_reasoning_effort=")]
    if expected is None:
        assert overrides == []
    else:
        assert len(overrides) == 1
        assert tomllib.loads(overrides[0]) == {"model_reasoning_effort": expected}


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "answer",
    [{"reply": "bad"}, {**ANSWER, "tasks": [{"component": "outside", "title": "x", "text": "x"}]}],
)
def test_invalid_answer_never_escapes(
    provider: str, answer: dict[str, object], tmp_path: Path
) -> None:
    rung = EXPERTS[provider]("model", Runner(provider, answer), effort=None, timeout_s=20)
    with pytest.raises(ExpertError):
        asyncio.run(
            rung.turn(
                ExpertRequest(tmp_path, {"api": tmp_path}, "hello"), session_id=None, handoff=None
            )
        )


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        del seconds


class Rung:
    def __init__(self, provider: str, *, down: bool = False) -> None:
        self.provider = provider
        self.name = provider
        self.down = down
        self.calls: list[tuple[str | None, str | None]] = []

    async def turn(
        self, request: ExpertRequest, *, session_id: str | None, handoff: str | None
    ) -> ExpertResult:
        del request
        self.calls.append((session_id, handoff))
        if self.down:
            msg = "down"
            raise ExpertError(msg)
        answer = ExpertAnswer.model_validate(ANSWER)
        return ExpertResult(
            answer, ExpertSession(0, self.provider, "session", answer.findings), None
        )

    async def probe(self) -> None:
        pass


def test_ladder_resumes_hands_off_and_skips_cooldown(tmp_path: Path) -> None:
    first, second, third = Rung("claude"), Rung("claude"), Rung("codex")
    ladder = LadderExpert(
        [first, second, third],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=2,
        handoff_turns=2,
        timeout_s=20,
    )
    turns = (
        ExpertTurn("old", "old answer"),
        ExpertTurn("one", "answer one"),
        ExpertTurn("two", "answer two"),
    )

    async def run() -> ExpertResult:
        initial = await ladder.turn(ExpertRequest(tmp_path, {"api": tmp_path}, "first"))
        again = await ladder.turn(
            ExpertRequest(tmp_path, {"api": tmp_path}, "next", session=initial.session)
        )
        first.down = True
        same = await ladder.turn(
            ExpertRequest(tmp_path, {"api": tmp_path}, "next", session=again.session)
        )
        second.down = True
        switched = await ladder.turn(
            ExpertRequest(tmp_path, {"api": tmp_path}, "next", session=same.session, turns=turns)
        )
        await ladder.turn(ExpertRequest(tmp_path, {"api": tmp_path}, "new"))
        return switched

    switched = asyncio.run(run())
    assert first.calls[1] == ("session", None)
    assert second.calls[0] == ("session", None)
    assert third.calls[0][0] is None
    handoff = third.calls[0][1]
    assert handoff is not None
    assert "Useful facts" in handoff
    assert "one" in handoff
    assert "two" in handoff
    assert "old" not in handoff
    assert len(first.calls) == 3
    assert len(second.calls) == 2
    assert switched.session.rung == 2


def test_ladder_reports_exhaustion(tmp_path: Path) -> None:
    ladder = LadderExpert(
        [Rung("claude", down=True), Rung("codex", down=True)],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=1,
        handoff_turns=2,
        timeout_s=20,
    )
    with pytest.raises(ExpertError, match=r"claude.*codex"):
        asyncio.run(ladder.turn(ExpertRequest(tmp_path, {"api": tmp_path}, "hello")))
