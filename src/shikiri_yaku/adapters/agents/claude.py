"""The Claude CLI: its Reasoner rung and its Expert turn, and nothing about any other agent."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, override

from pydantic import BaseModel, ConfigDict

from shikiri_yaku.adapters.agents.common import (
    BLOCKER_SCHEMA,
    WEB_COMMAND,
    WEB_SERVER,
    AgentAdapter,
    BlockerAnswer,
    CliExpert,
    CliRung,
)
from shikiri_yaku.domain.classification import CallRecord
from shikiri_yaku.domain.expert import ExpertAnswer  # noqa: TC001 -- pydantic reads the field type
from shikiri_yaku.ports import ExpertError

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.domain.expert import ExpertRequest
    from shikiri_yaku.ports import CommandResult

NAME: Final = "claude"
_TOOLS: Final = "WebSearch,WebFetch,Bash(command -v:*),Bash(brew info:*),Bash(brew search:*)"
_LOOKUPS: Final = (
    "You may look things up with your tools: a web search, a documentation page and read-only "
    "package queries. Install and change nothing."
)


class _ReasonerEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_error: bool
    structured_output: BlockerAnswer | None
    total_cost_usd: float | None = None
    duration_api_ms: int | None = None
    usage: dict[str, object] | None = None


class _ExpertEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    is_error: bool
    session_id: str
    structured_output: ExpertAnswer | None
    duration_api_ms: int | None = None
    total_cost_usd: float | None = None
    usage: dict[str, object] | None = None


class ClaudeRung(CliRung):
    """One Claude model with read-only web and package lookups, and no install tools.

    The rung passes an explicit tool list. `Read` is not on it: it cannot be confined to the
    scratch directory, and a model that can read any host file has a way to send what it reads
    out, including text injected through an escalation. `dontAsk` refuses any tool outside the
    list, since a headless call has no one to answer. `--setting-sources ""` loads no settings,
    hooks or CLAUDE.md. The effort, when the rung names one, is passed as `--effort`; the call
    reports its usage.
    """

    _adapter = NAME
    _lookups = _LOOKUPS

    @override
    def _argv(self, prompt: str, scratch: Path) -> list[str]:
        effort = [] if self._effort is None else ["--effort", self._effort]
        return [
            "claude",
            "-p",
            "--model",
            self._model,
            "--output-format",
            "json",
            "--json-schema",
            BLOCKER_SCHEMA,
            "--tools",
            _TOOLS,
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--no-session-persistence",
            *effort,
            prompt,
        ]

    @override
    def _answer(
        self, result: CommandResult, scratch: Path
    ) -> tuple[BlockerAnswer, CallRecord | None]:
        envelope = _ReasonerEnvelope.model_validate_json(result.stdout)
        if envelope.is_error or envelope.structured_output is None:
            self._fail("its call reported an error or gave no structured answer")
        return envelope.structured_output, _call_of(envelope)


def _call_of(envelope: _ReasonerEnvelope) -> CallRecord:
    return CallRecord(
        duration_ms=envelope.duration_api_ms,
        cost_usd=envelope.total_cost_usd,
        usage=envelope.usage,
    )


class ClaudeExpert(CliExpert):
    """One Claude model in the Expert ladder: a session, the web tool and the read-only scope."""

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
        web_config = {
            "mcpServers": {
                WEB_SERVER: {"command": WEB_COMMAND, "args": self._web_args(request)},
            }
        }
        argv = [
            "claude",
            "-p",
            "--model",
            self._model,
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
            json.dumps(web_config),
            "--allowedTools",
            f"mcp__{WEB_SERVER}__web_fetch",
        ]
        if effort:
            argv += ["--effort", effort]
        if session:
            argv += ["--resume", session]
        if request.scope:
            argv += ["--add-dir", *(str(path) for path in request.scope.values())]
        return argv

    @override
    def _answer(
        self, result: CommandResult, answer_file: Path
    ) -> tuple[ExpertAnswer, str, CallRecord | None]:
        envelope = _ExpertEnvelope.model_validate_json(result.stdout)
        if envelope.is_error or envelope.structured_output is None or not envelope.session_id:
            msg = f"{self.name}: no structured answer or session"
            raise ExpertError(msg)
        return (
            envelope.structured_output,
            envelope.session_id,
            CallRecord(
                duration_ms=envelope.duration_api_ms,
                cost_usd=envelope.total_cost_usd,
                usage=envelope.usage,
            ),
        )


AGENT: Final = AgentAdapter(name=NAME, reasoner=ClaudeRung, expert=ClaudeExpert)
