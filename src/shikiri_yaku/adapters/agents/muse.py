"""The Muse Code CLI: its Reasoner rung, and nothing about any other agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from pydantic import BaseModel, ConfigDict, ValidationError

from shikiri_yaku.adapters.agents.common import (
    SCHEMA_FILE,
    AgentAdapter,
    BlockerAnswer,
    CliRung,
)
from shikiri_yaku.domain.classification import CallRecord

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.ports import CommandResult

NAME: Final = "muse"
READ_ONLY_PROFILE: Final = ":read-only"
COMPLETED: Final = "run.terminal.completed"
FAILED: Final = "run.terminal.failed"
USAGE: Final = "run.usage"
TOKEN_FIELDS: Final = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
_TERMINALS: Final = frozenset({COMPLETED, FAILED})
_USAGE_TYPES: Final = frozenset({USAGE})
_LOOKUPS: Final = "Answer from the facts above alone. Run no command and look nothing up."


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    payload_type: str
    payload: dict[str, object]


class _Terminal(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    text: str
    reason: str | None = None


class _ModelUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    models: list[_ModelUsage]


class MuseRung(CliRung):
    """One Muse model, read-only, with no shell, web tool or reminder, and no lookups.

    `--permission-profile :read-only` refuses every write. It does not refuse a read, so the model
    can read any file the user can, as a Codex rung can. The profile cannot be combined with an
    approval mode, and a shell or MCP call under it would wait for an approval nobody gives, so the
    rung disables the shell and passes no MCP server. The effort, when the rung names one, is
    passed as `--reasoning-effort`. The answer is the text of the terminal record of the JSON-lines
    stream; the call reports the token counts summed over the models of the usage record.
    """

    _adapter = NAME
    _lookups = _LOOKUPS

    @override
    def _argv(self, prompt: str, scratch: Path) -> list[str]:
        effort = [] if self._effort is None else ["--reasoning-effort", self._effort]
        return [
            "muse",
            "exec",
            "--json",
            "--model",
            self._model,
            *effort,
            "--permission-profile",
            READ_ONLY_PROFILE,
            "--disable-shell",
            "--disable-web-tools",
            "--disable-reminders",
            "--no-session-log",
            "--output-schema",
            str(scratch / SCHEMA_FILE),
            prompt,
        ]

    @override
    def _check_exit(self, result: CommandResult) -> None:
        # keep: a failed run exits 1 and still prints its reason on the terminal record
        if result.returncode != 0 and (reason := _failure_reason(result.stdout)) is not None:
            self._fail(f"its run failed: {reason}")
        super()._check_exit(result)

    @override
    def _answer(
        self, result: CommandResult, scratch: Path
    ) -> tuple[BlockerAnswer, CallRecord | None]:
        records = self._records(result.stdout)
        terminal = _last(records, _TERMINALS)
        if terminal is None:
            self._fail("its output has no terminal record")
        outcome = _Terminal.model_validate(terminal.payload)
        if terminal.payload_type == FAILED:
            self._fail(f"its run failed: {outcome.reason or 'no reason was given'}")
        usage = _last(records, _USAGE_TYPES)
        return BlockerAnswer.model_validate_json(outcome.text), _call_of(usage)

    def _records(self, stdout: str) -> list[_Record]:
        try:
            return [_Record.model_validate_json(line) for line in stdout.splitlines()]
        except ValidationError:
            self._fail("its output has a line that is not a JSON record")


def _failure_reason(stdout: str) -> str | None:
    """Return the reason of a failed terminal record, or None when there is none."""
    records = [record for line in stdout.splitlines() if (record := _parse(line)) is not None]
    terminal = _last(records, _TERMINALS)
    if terminal is None or terminal.payload_type != FAILED:
        return None
    try:
        outcome = _Terminal.model_validate(terminal.payload)
    except ValidationError:
        return None
    return outcome.reason or "no reason was given"


def _parse(line: str) -> _Record | None:
    try:
        return _Record.model_validate_json(line)
    except ValidationError:
        return None


def _last(records: list[_Record], types: frozenset[str]) -> _Record | None:
    return next((r for r in reversed(records) if r.payload_type in types), None)


def _call_of(usage: _Record | None) -> CallRecord | None:
    if usage is None:
        return None
    models = _Usage.model_validate(usage.payload).models
    totals: dict[str, object] = {
        field: sum(getattr(model, field) for model in models) for field in TOKEN_FIELDS
    }
    return CallRecord(duration_ms=None, cost_usd=None, usage=totals)


AGENT: Final = AgentAdapter(name=NAME, reasoner=MuseRung, expert=None)
