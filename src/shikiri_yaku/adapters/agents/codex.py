"""The Codex CLI: its Reasoner rung and its Expert turn, and nothing about any other agent."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

from pydantic import BaseModel, ConfigDict, ValidationError

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
from shikiri_yaku.ports import CommandError, ExpertError, SketchError

if TYPE_CHECKING:
    from shikiri_yaku.domain.expert import ExpertAnswer, ExpertRequest
    from shikiri_yaku.ports import CommandResult, ExpertCommandRunner

NAME: Final = "codex"
THREAD_STARTED: Final = "thread.started"
CODEX_HOME_VARIABLE: Final = "CODEX_HOME"
DEFAULT_HOME: Final = ".codex"
GENERATED_IMAGES: Final = "generated_images"
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
PLAIN_NAME: Final = re.compile(r"[A-Za-z0-9_-]+")
_SKETCH_FAILED: Final = "codex did not finish the run"
_NO_THREAD: Final = "codex named no thread"
_NO_PICTURE: Final = "codex saved no picture"
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
        self, result: CommandResult, answer_file: Path, model: type[ExpertAnswer]
    ) -> tuple[ExpertAnswer, str, CallRecord | None]:
        events = [_CodexEvent.model_validate_json(line) for line in result.stdout.splitlines()]
        session = next((event.thread_id for event in events if event.type == THREAD_STARTED), None)
        if not isinstance(session, str) or not session:
            msg = f"{self.name}: no session"
            raise ExpertError(msg)
        return (
            model.model_validate_json(answer_file.read_text(encoding="utf-8")),
            session,
            _token_call(result),
        )


class CodexSketcher:
    """Draws a picture with one Codex model: a fresh thread, read-only, and no web tool.

    The description goes on standard input and the run starts in an empty directory. Codex keeps
    the picture in its own store, under the thread the run started, so the picture is found by
    that thread's id. The id and the file are checked before the path is returned.
    """

    def __init__(
        self,
        model: str,
        runner: ExpertCommandRunner,
        *,
        effort: str | None,
        timeout_s: float,
        store: Path | None = None,
    ) -> None:
        self._model = model
        self._runner = runner
        self._effort = effort
        self._timeout_s = timeout_s
        self._store = codex_store() if store is None else store

    async def draw(self, description: str) -> Path:
        with tempfile.TemporaryDirectory() as empty:
            try:
                result = await self._runner.run(
                    self._argv(),
                    timeout_s=self._timeout_s,
                    cwd=Path(empty),
                    stdin=description,
                )
            except CommandError:
                raise SketchError(_SKETCH_FAILED) from None
        if result.returncode != 0:
            raise SketchError(_SKETCH_FAILED)
        thread = _thread_id(result.stdout)
        if thread is None:
            raise SketchError(_NO_THREAD)
        picture = _newest_png(self._store / GENERATED_IMAGES / thread)
        if picture is None:
            raise SketchError(_NO_PICTURE)
        return picture

    def _argv(self) -> list[str]:
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
            "--skip-git-repo-check",
            "-m",
            self._model,
        ]
        if self._effort is not None:
            argv += ["-c", f"model_reasoning_effort={json.dumps(self._effort, ensure_ascii=False)}"]
        return argv


def codex_store() -> Path:
    """Return Codex's own directory: CODEX_HOME when it is set, else the default under the home."""
    home = os.environ.get(CODEX_HOME_VARIABLE)
    return Path(home) if home else Path.home() / DEFAULT_HOME


def _thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = _CodexEvent.model_validate_json(line)
        except ValidationError:
            continue
        if (
            event.type == THREAD_STARTED
            and event.thread_id
            and PLAIN_NAME.fullmatch(event.thread_id)
        ):
            return event.thread_id
    return None


def _newest_png(folder: Path) -> Path | None:
    """Return the newest PNG a run saved in its thread's folder, if it is a regular PNG file."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return None
        files = [path for path in folder.glob("*.png") if path.is_file() and not path.is_symlink()]
        if not files:
            return None
        newest = max(files, key=lambda path: path.stat().st_mtime)
        with newest.open("rb") as image:
            signature = image.read(len(PNG_SIGNATURE))
    except OSError:
        return None
    return newest if signature == PNG_SIGNATURE else None


AGENT: Final = AgentAdapter(
    name=NAME, reasoner=CodexRung, expert=CodexExpert, sketcher=CodexSketcher
)
