"""Muse Expert command, session, and output boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from shikiri_yaku.adapters.agents.muse import MuseExpert
from shikiri_yaku.app.expert_ladder import LadderExpert
from shikiri_yaku.domain.expert import ExpertRequest, ExpertSession, ExpertTurn
from shikiri_yaku.ports import CommandError, CommandResult, ExpertError
from tests.unit.test_expert import Clock, Rung

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class Runner:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[list[str]] = []
        self.prompts: list[str] = []
        self.directories: list[Path | None] = []
        self.scratch: list[Path] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout_s, env, stdin
        self.calls.append(list(argv))
        self.directories.append(cwd)
        schema = Path(argv[argv.index("--output-schema") + 1])
        prompt = Path(argv[argv.index("--prompt-file") + 1])
        self.scratch.append(schema.parent)
        self.prompts.append(prompt.read_text(encoding="utf-8"))
        assert schema.exists() and prompt.parent == schema.parent
        session = argv[argv.index("--session-id") + 1]
        if self.mode == "timeout":
            raise CommandError("timeout")
        if self.mode == "fixture":
            fixture = Path("tests/fixtures/muse/success-schema-turn1.jsonl")
            records = [json.loads(line) for line in fixture.read_text().splitlines()]
            for item in records:
                if item["stream"]["kind"] == "session":
                    item["stream"]["id"] = session
                if item["payload_type"] == "run.terminal.completed":
                    item["payload"]["text"] = json.dumps(
                        {"reply": "ok", "title": "title", "tasks": [], "findings": "facts"}
                    )
            return CommandResult(0, "\n".join(json.dumps(item) for item in records), "")
        answer: dict[str, object] = {
            "reply": "ok",
            "title": "title",
            "tasks": [],
            "findings": "facts",
        }
        if self.mode == "schema":
            answer = {"reply": "bad"}
        if self.mode == "scope":
            answer["tasks"] = [{"component": "outside", "title": "x", "text": "x"}]
        stream = {"kind": "session", "id": "different" if self.mode == "different" else session}
        if self.mode == "missing":
            stream = {"kind": "run", "id": session}
        record = {
            "stream": stream,
            "payload_type": "run.terminal.completed",
            "payload": {"text": json.dumps(answer)},
        }
        if self.mode == "failed":
            record["payload_type"] = "run.terminal.failed"
        if self.mode == "no_terminal":
            record["payload_type"] = "other"
        return CommandResult(1 if self.mode == "exit" else 0, json.dumps(record), "")


@pytest.mark.parametrize("session", [None, "stored"])
def test_muse_arguments_and_session(tmp_path: Path, session: str | None) -> None:
    runner = Runner()
    rung = MuseExpert("model", runner, effort="high", timeout_s=20)
    request = ExpertRequest(tmp_path, {"api": Path("/component")}, "hello")
    result = asyncio.run(rung.turn(request, session_id=session, handoff=None))
    argv = runner.calls[0]
    passed = argv[argv.index("--session-id") + 1]
    if session is None:
        UUID(passed)
    else:
        assert passed == session
    assert result.session.session_id == passed
    scratch = runner.scratch[0]
    config = argv[argv.index("-c") + 1]
    assert argv == [
        "muse",
        "exec",
        "--json",
        "--model",
        "model",
        "--reasoning-effort",
        "high",
        "--approval-mode",
        "never",
        "--trust-workspace",
        "--disable-shell",
        "--disable-web-tools",
        "--disable-reminders",
        "--output-schema",
        str(scratch / "schema.json"),
        "--session-id",
        passed,
        "-c",
        config,
        "--prompt-file",
        str(scratch / "prompt.md"),
    ]
    assert config.startswith("mcpServers.expert_web=")
    server = json.loads(config.split("=", 1)[1])
    assert server["command"] == "shikiri-yaku"
    assert server["args"][0] == "_expert-web"
    assert runner.directories == [tmp_path] and scratch.parent == tmp_path
    assert "hello" in runner.prompts[0]
    assert not scratch.exists()


@pytest.mark.parametrize(
    "mode", ["different", "missing", "schema", "scope", "failed", "no_terminal", "exit", "timeout"]
)
def test_failures(tmp_path: Path, mode: str) -> None:
    rung = MuseExpert("model", Runner(mode), effort=None, timeout_s=20)
    with pytest.raises(ExpertError):
        asyncio.run(
            rung.turn(
                ExpertRequest(tmp_path, {"api": Path("/component")}, "hello"),
                session_id=None,
                handoff=None,
            )
        )


def test_temporary_component_refused(tmp_path: Path) -> None:
    runner = Runner()
    rung = MuseExpert("model", runner, effort=None, timeout_s=20)
    with pytest.raises(ExpertError, match="temporary-api"):
        asyncio.run(
            rung.turn(
                ExpertRequest(
                    tmp_path, {"safe": Path("/component"), "temporary-api": tmp_path}, "hello"
                ),
                session_id=None,
                handoff=None,
            )
        )
    assert not runner.calls


def test_ladder_resumes_muse_and_hands_off(tmp_path: Path) -> None:
    runner = Runner()
    muse = MuseExpert("model", runner, effort=None, timeout_s=20)
    fallback = Rung("claude")
    ladder = LadderExpert(
        [muse, fallback],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=1,
        handoff_turns=1,
        timeout_s=20,
    )
    scope = {"api": Path("/component")}

    async def run() -> None:
        first = await ladder.turn(ExpertRequest(tmp_path, scope, "first"))
        second = await ladder.turn(ExpertRequest(tmp_path, scope, "second", session=first.session))
        passed = runner.calls[1][runner.calls[1].index("--session-id") + 1]
        assert passed == first.session.session_id
        assert second.session.session_id == first.session.session_id
        runner.mode = "exit"
        switched = await ladder.turn(
            ExpertRequest(
                tmp_path,
                scope,
                "third",
                session=second.session,
                turns=(ExpertTurn("earlier", "reply"),),
            )
        )
        assert switched.session.provider == "claude"

    asyncio.run(run())
    assert fallback.calls[0][0] is None
    assert fallback.calls[0][1] is not None
    assert "facts" in fallback.calls[0][1] and "earlier" in fallback.calls[0][1]
    assert runner.directories == [tmp_path, tmp_path, tmp_path]
    assert all(path.parent == tmp_path and not path.exists() for path in runner.scratch)


def test_fixture_stream_and_handoff_to_muse(tmp_path: Path) -> None:
    runner = Runner("fixture")
    rung = MuseExpert("model", runner, effort=None, timeout_s=20)
    ladder = LadderExpert(
        [rung],
        Clock(),
        cooldown=timedelta(minutes=5),
        max_parallel=1,
        handoff_turns=1,
        timeout_s=20,
    )
    previous = ExpertSession(0, "claude", "old", "previous facts")
    request = ExpertRequest(
        tmp_path,
        {"api": Path("/component")},
        "next",
        session=previous,
        turns=(ExpertTurn("earlier", "reply"),),
    )
    result = asyncio.run(ladder.turn(request))
    assert result.session.provider == "muse"
    assert result.session.session_id != "old"
    assert "previous facts" in runner.prompts[0] and "earlier" in runner.prompts[0]
    assert result.call is not None
    again = asyncio.run(ladder.turn(ExpertRequest(tmp_path, request.scope, "new")))
    assert again.session.session_id != result.session.session_id
