"""The Muse rung against a fake runner that plays `muse exec --json` with the recorded streams."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, override

import pytest

from shikiri_yaku.adapters.agents.muse import MuseRung
from shikiri_yaku.app.reasoner_ladder import LadderReasoner
from shikiri_yaku.bootstrap import reasoner_rungs
from shikiri_yaku.domain.classification import Assessment, CallRecord, Verdict
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner, ReasonerError
from shikiri_yaku.settings import ReasonerSettings
from tests.support.clock import NapClock

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.ports import ReasonerRung

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "muse"
_MODEL: Final = "muse-ember-1.0"
_TIMEOUT_S: Final = 120
_INPUT_MAX_CHARS: Final = 4000
_FACTS: Final = ["OS: macOS 15", "package managers: brew"]
_ANSWER: Final = {
    "verdict": "host_tool",
    "tool": "ffmpeg",
    "manager": "brew",
    "package": "ffmpeg",
    "why": "ffmpeg is not on the PATH",
    "suggestion": "install ffmpeg with brew",
}
_ASSESSED: Final = Assessment(
    verdict=Verdict.HOST_TOOL,
    note="install ffmpeg with brew",
    tool="ffmpeg",
    manager="brew",
    package="ffmpeg",
    why="ffmpeg is not on the PATH",
)
_RECORDED_USAGE: Final[dict[str, object]] = {
    "input_tokens": 62242,
    "cached_tokens": 16244,
    "output_tokens": 753,
    "reasoning_tokens": 587,
}
_RECORDED_MODEL: Final[dict[str, object]] = {
    "model": "muse-ember-1.0",
    "input_tokens": 62242,
    "cached_tokens": 16244,
    "output_tokens": 753,
    "reasoning_tokens": 587,
    "calls": 2,
}
_SECOND_MODEL: Final[dict[str, object]] = {
    "model": "muse-other-1.0",
    "input_tokens": 100,
    "cached_tokens": 10,
    "output_tokens": 5,
    "reasoning_tokens": 2,
    "calls": 1,
}
_SIGNAL: Final = Signal(
    project="api",
    kind=Kind.ESCALATION,
    source_id="esc-1",
    reason="blocked",
    text="the build needs ffmpeg",
    escalation_id="esc-1",
)
_FLAGS: Final = [
    ("exec", None),
    ("--json", None),
    ("--model", _MODEL),
    ("--permission-profile", ":read-only"),
    ("--disable-shell", None),
    ("--disable-web-tools", None),
    ("--disable-reminders", None),
    ("--no-session-log", None),
]
_NEVER_PASSED: Final = (
    "--disable-write",
    "--trust-workspace",
    "--yolo",
    "--disable-sandbox",
    "--disable-approval",
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _records(stream: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.splitlines()]


def _lines(records: Sequence[dict[str, Any]]) -> str:
    return "".join(f"{json.dumps(record)}\n" for record in records)


def _recorded(answer: str, *, models: list[dict[str, object]] | None = None) -> str:
    """The recorded success stream, with its final answer and its usage models replaced.

    The record shapes and the usage are the recorded ones; only the answer text is synthetic,
    because no recorded run answered the Reasoner's schema.
    """
    records = _records(_fixture("success-schema-turn1.jsonl"))
    for record in records:
        if record["payload_type"] == "run.terminal.completed":
            record["payload"]["text"] = answer
        if record["payload_type"] == "run.usage" and models is not None:
            record["payload"]["models"] = models
    return _lines(records)


def _without(stream: str, payload_type: str) -> str:
    return _lines([r for r in _records(stream) if r["payload_type"] != payload_type])


def _has_pair(argv: Sequence[str], flag: str, value: str | None) -> bool:
    if value is None:
        return flag in argv
    return any(a == flag and b == value for a, b in itertools.pairwise(argv))


def _value(argv: Sequence[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _snapshot(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in directory.iterdir()}


class _Muse(CommandRunner):
    """Plays `muse exec`: records each call, then returns the given output, exit code or error."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        error: CommandError | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.error = error
        self.argv: list[str] = []
        self.cwd: Path | None = None
        self.timeout_s: float | None = None
        self.scratch_files: dict[str, str] = {}

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
        self.timeout_s = timeout_s
        self.cwd = cwd
        if cwd is not None:
            self.scratch_files = await asyncio.to_thread(_snapshot, cwd)
        if self.error is not None:
            raise self.error
        return CommandResult(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _rung(runner: CommandRunner, root: Path, *, effort: str | None = None) -> MuseRung:
    return MuseRung(
        _MODEL,
        runner,
        effort=effort,
        timeout_s=_TIMEOUT_S,
        input_max_chars=_INPUT_MAX_CHARS,
        scratch_root=root,
    )


def _assess(rung: ReasonerRung, signal: Signal = _SIGNAL) -> Assessment:
    return asyncio.run(rung.assess_blocker(signal, _FACTS))


@pytest.mark.parametrize("effort", [None, "high"])
def test_the_argument_list_is_fixed_with_the_prompt_last(
    effort: str | None, tmp_path: Path
) -> None:
    runner = _Muse(stdout=_recorded(json.dumps(_ANSWER)))
    _assess(_rung(runner, tmp_path, effort=effort))

    assert runner.argv[0] == "muse"
    assert all(_has_pair(runner.argv, flag, value) for flag, value in _FLAGS)
    assert [flag for flag in _NEVER_PASSED if flag in runner.argv] == []
    assert runner.argv[-1].startswith("You assess one blocker")
    assert ("--reasoning-effort" in runner.argv) == (effort is not None)
    if effort is not None:
        assert _has_pair(runner.argv, "--reasoning-effort", effort)


def test_the_schema_is_read_from_the_scratch_directory_the_rung_runs_in(tmp_path: Path) -> None:
    runner = _Muse(stdout=_recorded(json.dumps(_ANSWER)))
    _assess(_rung(runner, tmp_path))

    assert runner.cwd is not None
    assert runner.cwd.parent == tmp_path
    assert _value(runner.argv, "--output-schema") == str(runner.cwd / "schema.json")
    assert set(json.loads(runner.scratch_files["schema.json"])["properties"]) == {
        "verdict",
        "tool",
        "manager",
        "package",
        "why",
        "suggestion",
    }
    assert not runner.cwd.exists()


def test_a_valid_recorded_answer_is_the_assessment_with_the_summed_usage(tmp_path: Path) -> None:
    assessed = _assess(_rung(_Muse(stdout=_recorded(json.dumps(_ANSWER))), tmp_path))

    assert assessed == dataclasses.replace(
        _ASSESSED,
        call=CallRecord(duration_ms=None, cost_usd=None, usage=_RECORDED_USAGE),
    )


def test_the_usage_is_summed_over_every_model_of_the_run(tmp_path: Path) -> None:
    stream = _recorded(json.dumps(_ANSWER), models=[_RECORDED_MODEL, _SECOND_MODEL])

    assessed = _assess(_rung(_Muse(stdout=stream), tmp_path))

    assert assessed.call == CallRecord(
        duration_ms=None,
        cost_usd=None,
        usage={
            "input_tokens": 62342,
            "cached_tokens": 16254,
            "output_tokens": 758,
            "reasoning_tokens": 589,
        },
    )


def test_a_stream_without_a_usage_record_reports_no_call(tmp_path: Path) -> None:
    stream = _without(_recorded(json.dumps(_ANSWER)), "run.usage")

    assessed = _assess(_rung(_Muse(stdout=stream), tmp_path))

    assert assessed.call is None
    assert assessed.verdict is Verdict.HOST_TOOL


def test_a_failed_run_that_exits_one_names_its_reason_and_the_rung(tmp_path: Path) -> None:
    # keep: the CLI exits 1 when its run fails, so the recorded reason must survive that code
    runner = _Muse(stdout=_fixture("failed-model-missing.jsonl"), returncode=1)

    with pytest.raises(ReasonerError) as raised:
        _assess(_rung(runner, tmp_path))

    assert f"muse:{_MODEL} gave no usable answer" in str(raised.value)
    assert "does not exist or you lack access" in str(raised.value)


_FAILURES: Final = [
    pytest.param(
        {"stdout": _fixture("failed-model-missing.jsonl"), "returncode": 1}, id="failed-run"
    ),
    pytest.param({"stdout": _fixture("interrupted-sigterm.jsonl")}, id="no-terminal-record"),
    pytest.param(
        {"stdout": _fixture("success-schema-turn1.jsonl")},
        id="answer-outside-the-schema",
    ),
    pytest.param(
        {"stderr": _fixture("failed-api-key-rejected.stderr.txt"), "returncode": 1},
        id="non-zero-exit",
    ),
    pytest.param(
        {"stdout": _recorded(json.dumps(_ANSWER)), "returncode": 1},
        id="completed-run-with-non-zero-exit",
    ),
    pytest.param(
        {"error": CommandError("muse did not finish in 120 seconds and was killed")},
        id="timeout",
    ),
    pytest.param(
        {"stdout": _fixture("failed-missing-credentials.stderr.txt")},
        id="stdout-not-json",
    ),
]


@pytest.mark.parametrize("kwargs", _FAILURES)
def test_every_unusable_run_is_a_reasoner_error_naming_the_rung(
    kwargs: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(ReasonerError, match=re.escape(f"muse:{_MODEL}")):
        _assess(_rung(_Muse(**kwargs), tmp_path))


def test_the_probe_runs_only_the_version_flag(tmp_path: Path) -> None:
    runner = _Muse()

    asyncio.run(_rung(runner, tmp_path).probe())

    assert runner.argv == ["muse", "--version"]


def test_a_probe_whose_muse_cannot_start_names_the_rung_with_its_effort(tmp_path: Path) -> None:
    runner = _Muse(error=CommandError("no such file"))

    with pytest.raises(ReasonerError, match=re.escape(f"muse:{_MODEL}:high")):
        asyncio.run(_rung(runner, tmp_path, effort="high").probe())


def test_a_probe_whose_version_flag_fails_is_a_reasoner_error(tmp_path: Path) -> None:
    runner = _Muse(returncode=127)

    with pytest.raises(ReasonerError, match=re.escape(f"muse:{_MODEL}")):
        asyncio.run(_rung(runner, tmp_path).probe())


def test_a_muse_rung_is_named_with_its_model_and_its_effort(tmp_path: Path) -> None:
    config = ReasonerSettings.model_validate({"ladder": [f"muse:{_MODEL}", f"muse:{_MODEL}:high"]})

    rungs = reasoner_rungs(config, _Muse(), tmp_path)

    assert [rung.name for rung in rungs] == [f"muse:{_MODEL}", f"muse:{_MODEL}:high"]


def test_the_ladder_skips_a_failed_muse_rung_and_answers_from_the_next(tmp_path: Path) -> None:
    config = ReasonerSettings.model_validate({"ladder": [f"muse:{_MODEL}", "codex:gpt-5"]})
    rungs = reasoner_rungs(config, _MuseDownCodexUp(), tmp_path)
    ladder = LadderReasoner(rungs, NapClock(), cooldown=timedelta(minutes=5), max_parallel=2)

    assessed = asyncio.run(ladder.assess_blocker(_SIGNAL, _FACTS))

    assert dataclasses.replace(assessed, call=None) == _ASSESSED
    states = {state.name: state for state in ladder.rung_states()}
    assert states[f"muse:{_MODEL}"].last_failure is not None
    assert states[f"muse:{_MODEL}"].cooling_until is not None


class _MuseDownCodexUp(CommandRunner):
    """Muse exits with an error; Codex writes the answer file that its rung reads."""

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        if argv[0] == "muse":
            return CommandResult(returncode=1, stdout="", stderr="")
        answer = _value(argv, "--output-last-message")
        await asyncio.to_thread(Path(answer).write_text, json.dumps(_ANSWER), encoding="utf-8")
        return CommandResult(returncode=0, stdout="", stderr="")
