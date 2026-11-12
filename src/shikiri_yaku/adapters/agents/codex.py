"""The Codex CLI: its Reasoner rung and its Expert turn, and nothing about any other agent."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final, override

from pydantic import BaseModel, ConfigDict

from shikiri_yaku.adapters.agents.common import (
    ANSWER_FILE,
    SCHEMA_FILE,
    WEB_COMMAND,
    WEB_SERVER,
    AgentAdapter,
    BlockerAnswer,
    CliExpert,
    CliRung,
)
from shikiri_yaku.domain.classification import CallRecord
from shikiri_yaku.domain.expert import ExpertAnswer
from shikiri_yaku.ports import ExpertError

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.domain.expert import ExpertRequest
    from shikiri_yaku.ports import CommandResult

NAME: Final = "codex"
THREAD_STARTED: Final = "thread.started"
_LOOKUPS: Final = "Answer from the facts above alone. Run no command and look nothing up."
_TOKENS: Final = re.compile(r"tokens used:?\s+(\d[\d,]*)(?![\w.])")


class _CodexEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: str
    thread_id: str | None = None


def _token_call(result: CommandResult) -> CallRecord | None:
    counts = _TOKENS.findall(f"{result.stderr}\n{result.stdout}")
    if not counts:
        return None
    usage: dict[str, object] = {"tokens_used": int(counts[-1].replace(",", ""))}
    return CallRecord(duration_ms=None, cost_usd=None, usage=usage)


class CodexRung(CliRung):
    """One Codex model, read-only in the scratch directory, with no lookups.

    `codex exec` has no flag for the effort, so the effort of a rung is not passed to it. The call
    reports the last token count Codex prints, which comes before its answer, and no duration or
    cost. Codex cannot limit its shell to named commands, and web search is off unless `--search`
    is given; this adapter never gives it, so the rung has no lookups at all. `-s read-only` stops
    writes only: the model can still read files on the host. That is accepted, because it writes
    nothing and its answer is validated here.
    """

    _adapter = NAME
    _lookups = _LOOKUPS

    @override
    def _argv(self, prompt: str, scratch: Path) -> list[str]:
        return [
            "codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-m",
            self._model,
            "-C",
            str(scratch),
            "--output-schema",
            str(scratch / SCHEMA_FILE),
            "--output-last-message",
            str(scratch / ANSWER_FILE),
            prompt,
        ]

    @override
    def _answer(
        self, result: CommandResult, scratch: Path
    ) -> tuple[BlockerAnswer, CallRecord | None]:
        try:
            text = (scratch / ANSWER_FILE).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            self._fail("it wrote no answer file")
        return BlockerAnswer.model_validate_json(text), _token_call(result)


class CodexExpert(CliExpert):
    """One Codex model in the Expert ladder: a thread, the web tool and a read-only sandbox."""

    provider = NAME

    @override
    def _argv(
        self,
        request: ExpertRequest,
        session: str | None,
        schema: str,
        schema_file: Path,
        answer_file: Path,
    ) -> list[str]:
        effort = request.effort or self._effort
        web_args = self._web_args(request)
        argv = [
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
            f'mcp_servers.{WEB_SERVER}={{command="{WEB_COMMAND}",args='
            + json.dumps(web_args)
            + ',default_tools_approval_mode="approve"}',
            "--skip-git-repo-check",
            "-m",
            self._model,
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(answer_file),
        ]
        if effort is not None:
            argv += ["-c", f"model_reasoning_effort={json.dumps(effort, ensure_ascii=False)}"]
        if session:
            argv += ["resume", session]
        return argv

    @override
    def _answer(
        self, result: CommandResult, answer_file: Path
    ) -> tuple[ExpertAnswer, str, CallRecord | None]:
        events = [_CodexEvent.model_validate_json(line) for line in result.stdout.splitlines()]
        session = next((event.thread_id for event in events if event.type == THREAD_STARTED), None)
        if not isinstance(session, str) or not session:
            msg = f"{self.name}: no session"
            raise ExpertError(msg)
        return (
            ExpertAnswer.model_validate_json(answer_file.read_text(encoding="utf-8")),
            session,
            _token_call(result),
        )


AGENT: Final = AgentAdapter(name=NAME, reasoner=CodexRung, expert=CodexExpert)
