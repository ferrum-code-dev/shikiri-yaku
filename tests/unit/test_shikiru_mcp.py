"""The Shikiru MCP client against a fake session and payloads recorded from Shikiru's MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, override

import pytest
from mcp import StdioServerParameters, types
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from shikiri_yaku.adapters.shikiru_mcp import (
    DECLARED_TOOLS,
    SHIKIRU_STATE_DIRECTORY,
    ShikiruMcpClient,
    probe_link,
    stdio_connector,
)
from shikiri_yaku.ports import (
    AckStatus,
    CommandError,
    CommandResult,
    CommandRunner,
    DoctorCheck,
    ShikiruLinkDownError,
    ShikiruProtocolError,
    ShikiruRejectedError,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
    from datetime import timedelta
    from typing import TextIO

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_BACKOFF_INITIAL_S: Final = 1
_BACKOFF_MAX_S: Final = 5
_CALL_TIMEOUT_S: Final = 30


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _reply(structured: object, *, is_error: bool = False, text: str = "") -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,  # pyright: ignore[reportArgumentType]
        isError=is_error,
    )


class FakeSession:
    def __init__(self, replies: dict[str, CallToolResult] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any], timedelta]] = []
        self.broken = False
        self.offered: frozenset[str] = DECLARED_TOOLS

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name=name, inputSchema={"type": "object"}) for name in self.offered]
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: timedelta
    ) -> CallToolResult:
        self.calls.append((name, arguments, read_timeout_seconds))
        if self.broken:
            message = "the server process is gone"
            raise ConnectionError(message)
        return self.replies[name]


class FakeLink:
    """Hands out one session per connection and refuses the first `failures` attempts."""

    def __init__(self, sessions: list[FakeSession], failures: int = 0) -> None:
        self.sessions = sessions
        self.failures = failures
        self.attempts = 0
        self.connected = asyncio.Event()

    def __call__(self) -> Any:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[ToolSession]:
        self.attempts += 1
        if self.attempts <= self.failures:
            message = "cannot start shikiru"
            raise OSError(message)
        self.connected.set()
        yield self.sessions[self.attempts - self.failures - 1]


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 28, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeRunner(CommandRunner):
    def __init__(self, result: CommandResult | CommandError) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), timeout_s))
        if isinstance(self.result, CommandError):
            raise self.result
        return self.result


_START_ARGV: Final = ["shikiru", "--project", "/work/api", "start"]
_START_TIMEOUT_S: Final = 120
_STARTED: Final = CommandResult(returncode=0, stdout="started", stderr="")
_DOCTOR_ARGV: Final = ["shikiru", "--project", "/work/api", "--json", "doctor"]


def _client(
    link: Callable[[], Any], clock: FakeClock, runner: FakeRunner | None = None
) -> ShikiruMcpClient:
    return ShikiruMcpClient(
        link,
        clock,
        backoff_initial_s=_BACKOFF_INITIAL_S,
        backoff_max_s=_BACKOFF_MAX_S,
        call_timeout_s=_CALL_TIMEOUT_S,
        runner=runner or FakeRunner(_STARTED),
        start_argv=_START_ARGV,
        start_timeout_s=_START_TIMEOUT_S,
        doctor_argv=_DOCTOR_ARGV,
    )


async def _with_link(
    session: FakeSession,
    scenario: Callable[[ShikiruMcpClient], Awaitable[None]],
    runner: FakeRunner | None = None,
) -> None:
    link = FakeLink([session])
    client = _client(link, FakeClock(), runner)
    supervisor = asyncio.create_task(client.supervise())
    try:
        await link.connected.wait()
        await scenario(client)
    finally:
        supervisor.cancel()


def _run(
    session: FakeSession,
    scenario: Callable[[ShikiruMcpClient], Awaitable[None]],
    runner: FakeRunner | None = None,
) -> None:
    asyncio.run(_with_link(session, scenario, runner))


def test_the_s1_escalation_payload_is_read_with_its_decision_fields() -> None:
    session = FakeSession(
        {
            "get_escalation": _reply(_recorded("get_escalation")),
            "list_escalations": _reply(_recorded("list_escalations")),
        }
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        one = await client.get_escalation("E-1")
        listed = await client.list_escalations()
        assert one is not None
        assert listed == [one]
        assert (one.escalation.id, one.escalation.task_id, one.escalation.status) == (
            "E-1",
            "T-1",
            "open",
        )
        assert one.escalation.reason == "decision_required"
        assert one.escalation.resolution is None
        assert one.question == "Which behavior should remain?"
        assert [(o.label, o.recommended, o.needs_input) for o in one.agent_options] == [
            ("Keep existing behavior", True, False),
            ("Change behavior", False, False),
        ]
        assert [(r.kind, r.recommended) for r in one.resolutions] == [
            ("answer", True),
            ("cancel", False),
        ]
        assert one.packet is not None
        assert "## Question" in one.packet

    _run(session, scenario)


def test_status_events_and_tasks_are_read() -> None:
    session = FakeSession(
        {
            "status": _reply(_recorded("status")),
            "wait_for_events": _reply(_recorded("wait_for_events")),
            "list_tasks": _reply(_recorded("list_tasks")),
            "get_task": _reply(_recorded("get_task")),
        }
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        status = await client.status()
        assert status.engine_up is False
        window = status.quotas["claude"][0]
        assert (window.used_pct, window.resets_at) == (
            42.5,
            datetime(2026, 10, 28, 13, tzinfo=UTC),
        )

        events = await client.wait_for_events(40, 25)
        assert [(e.id, e.kind, e.task_id) for e in events] == [
            (41, "escalation", "T-1"),
            (42, "engine_stopped", None),
        ]
        assert events[0].data == {"escalation_id": "E-1", "reason": "decision_required"}

        (summary,) = await client.list_tasks([TaskStatus.ESCALATED])
        detail = await client.get_task("T-1")
        assert detail is not None
        assert detail.task.id == summary.id
        assert (summary.id, summary.parent_id, summary.status, summary.title) == (
            "T-1",
            None,
            TaskStatus.ESCALATED,
            "decide",
        )

    _run(session, scenario)

    names = [name for name, _, _ in session.calls]
    assert names == ["status", "wait_for_events", "list_tasks", "get_task"]
    assert session.calls[1][1] == {"since_id": 40, "timeout_s": 25}
    assert session.calls[1][2].total_seconds() == 25 + _CALL_TIMEOUT_S
    assert session.calls[2][1] == {"statuses": ["escalated"]}


def test_an_unknown_escalation_or_task_is_none() -> None:
    session = FakeSession(
        {"get_escalation": _reply({"result": None}), "get_task": _reply({"result": None})}
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        assert await client.get_escalation("E-9") is None
        assert await client.get_task("T-9") is None

    _run(session, scenario)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(_reply(None), id="no structured content"),
        pytest.param(_reply({"engine_up": "maybe"}), id="wrong type"),
        pytest.param(_reply({"quotas": {}}), id="missing field"),
    ],
)
def test_an_unreadable_result_is_a_protocol_error(bad: CallToolResult) -> None:
    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruProtocolError):
            await client.status()

    _run(FakeSession({"status": bad}), scenario)


def test_fields_and_values_shikiru_adds_still_parse() -> None:
    status: dict[str, Any] = {"engine_up": True, "quotas": {}, "contract": 2}
    tasks = _recorded("list_tasks")
    tasks["result"][0]["status"] = "paused"
    escalation = _recorded("get_escalation")
    escalation["result"]["resolutions"][1]["kind"] = "pause"
    session = FakeSession(
        {
            "status": _reply(status),
            "list_tasks": _reply(tasks),
            "get_escalation": _reply(escalation),
        }
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        assert (await client.status()).engine_up is True
        (summary,) = await client.list_tasks()
        assert summary.status == "paused"
        one = await client.get_escalation("E-1")
        assert one is not None
        assert [resolution.kind for resolution in one.resolutions] == ["answer", "pause"]

    _run(session, scenario)


def test_a_payload_missing_a_field_the_operator_reads_is_a_protocol_error() -> None:
    tasks = _recorded("list_tasks")
    del tasks["result"][0]["status"]
    escalation = _recorded("get_escalation")
    del escalation["result"]["resolutions"]
    session = FakeSession({"list_tasks": _reply(tasks), "get_escalation": _reply(escalation)})

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruProtocolError):
            await client.list_tasks()
        with pytest.raises(ShikiruProtocolError):
            await client.get_escalation("E-1")

    _run(session, scenario)


def test_a_tool_error_is_a_rejection_with_shikiru_s_message() -> None:
    refused = _reply(None, is_error=True, text="escalation E-1 is already resolved")

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruRejectedError, match="already resolved"):
            await client.get_escalation("E-1")

    _run(FakeSession({"get_escalation": refused}), scenario)


def test_every_verb_raises_link_down_before_the_link_is_up() -> None:
    client = _client(FakeLink([]), FakeClock())

    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError):
            await client.status()
        with pytest.raises(ShikiruLinkDownError):
            await client.wait_for_events(0, 1)

    asyncio.run(scenario())


def test_a_lost_link_raises_link_down_until_it_reconnects() -> None:
    first = FakeSession()
    second = FakeSession({"status": _reply({"engine_up": True, "quotas": {}})})
    link = FakeLink([first, second])
    clock = FakeClock()
    client = _client(link, clock)

    async def scenario() -> None:
        supervisor = asyncio.create_task(client.supervise())
        try:
            await link.connected.wait()
            link.connected.clear()
            first.broken = True
            with pytest.raises(ShikiruLinkDownError, match="gone"):
                await client.status()
            with pytest.raises(ShikiruLinkDownError):
                await client.status()
            assert len(first.calls) == 1
            await link.connected.wait()
            assert (await client.status()).engine_up is True
        finally:
            supervisor.cancel()

    asyncio.run(scenario())
    assert clock.sleeps == [_BACKOFF_INITIAL_S]


def test_reconnecting_waits_longer_after_each_failure_up_to_a_limit() -> None:
    failures = 5
    link = FakeLink([FakeSession()], failures=failures)
    clock = FakeClock()
    client = _client(link, clock)

    async def scenario() -> None:
        supervisor = asyncio.create_task(client.supervise())
        try:
            await link.connected.wait()
        finally:
            supervisor.cancel()

    asyncio.run(scenario())
    assert clock.sleeps == [1, 2, 4, 5, 5]


def _sdk_server(structured: dict[str, Any] | None) -> Server[Any]:
    schema = {
        "type": "object",
        "properties": {"engine_up": {"type": "boolean"}},
        "required": ["engine_up"],
    }
    server: Server[Any] = Server("shikiru")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name="status", inputSchema={"type": "object"}, outputSchema=schema)]

    @server.call_tool(validate_input=False)
    async def call_tool(_name: str, _arguments: dict[str, Any]) -> CallToolResult:
        return CallToolResult(content=[], structuredContent=structured)

    return server


@pytest.mark.parametrize(
    "structured",
    [
        pytest.param({"engine_up": "invalid"}, id="breaks the output schema"),
        pytest.param(None, id="no structured content"),
    ],
)
def test_the_sdk_rejecting_a_result_is_a_protocol_error_and_the_link_stays_up(
    structured: dict[str, Any] | None,
) -> None:
    connected = asyncio.Event()

    @asynccontextmanager
    async def connect() -> AsyncGenerator[ToolSession]:
        async with create_connected_server_and_client_session(_sdk_server(structured)) as session:
            connected.set()
            yield session

    client = _client(connect, FakeClock())

    async def scenario() -> None:
        supervisor = asyncio.create_task(client.supervise())
        try:
            await connected.wait()
            for _ in range(2):
                with pytest.raises(ShikiruProtocolError):
                    await client.status()
        finally:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor

    asyncio.run(scenario())


def test_a_rejected_ack_becomes_a_rejected_error_with_shikirus_message() -> None:
    rejected = {"id": 7, "status": "rejected", "result": None, "message": "E-1 is already resolved"}
    session = FakeSession({"resolve_escalation": _reply(rejected)})

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruRejectedError, match="E-1 is already resolved"):
            await client.resolve_escalation("E-1", "answer", "yes")

    _run(session, scenario)
    assert session.calls[0][1] == {
        "escalation_id": "E-1",
        "resolution": {"kind": "answer", "text": "yes"},
    }


def test_a_queued_ack_is_returned_as_queued_and_a_submit_carries_its_task_id() -> None:
    queued = {
        "id": 9,
        "status": "queued",
        "result": {"task_id": "T-4"},
        "message": "applies when the engine starts",
    }
    session = FakeSession({"submit_task": _reply(queued)})

    async def scenario(client: ShikiruMcpClient) -> None:
        ack = await client.submit_task("add a login page")
        assert (ack.id, ack.status, ack.task_id) == (9, AckStatus.QUEUED, "T-4")

    _run(session, scenario)


def test_a_done_ack_with_fields_shikiru_added_is_read() -> None:
    done = {"id": 3, "status": "done", "result": None, "message": None, "later": 1}
    session = FakeSession({"resolve_escalation": _reply(done)})

    async def scenario(client: ShikiruMcpClient) -> None:
        ack = await client.resolve_escalation("E-1", "retry")
        assert (ack.id, ack.status, ack.task_id) == (3, AckStatus.DONE, None)

    _run(session, scenario)


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (CommandResult(returncode=1, stdout="", stderr="no tmux\n"), ShikiruRejectedError),
        (CommandError("did not finish"), CommandError),
    ],
)
def test_start_engine_fails_with_the_commands_own_message(
    outcome: CommandResult | CommandError, error: type[Exception]
) -> None:
    runner = FakeRunner(outcome)

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(error):
            await client.start_engine()

    _run(FakeSession(), scenario, runner)
    assert runner.calls == [(_START_ARGV, _START_TIMEOUT_S)]


def test_start_engine_runs_the_stored_argument_list() -> None:
    runner = FakeRunner(_STARTED)

    async def scenario(client: ShikiruMcpClient) -> None:
        await client.start_engine()

    _run(FakeSession(), scenario, runner)
    assert runner.calls == [(_START_ARGV, _START_TIMEOUT_S)]


_DOCTOR_REPORT: Final = json.dumps(
    {
        "ok": False,
        "checks": [
            {"name": "tmux", "ok": True, "detail": "session found"},
            {"name": "agent_cli", "ok": False, "detail": "claude not on PATH"},
        ],
    }
)


def test_doctor_returns_only_the_checks_that_failed() -> None:
    runner = FakeRunner(CommandResult(returncode=1, stdout=_DOCTOR_REPORT, stderr=""))
    found: list[DoctorCheck] = []

    async def scenario(client: ShikiruMcpClient) -> None:
        found.extend(await client.doctor())

    _run(FakeSession(), scenario, runner)
    assert [(check.name, check.detail) for check in found] == [("agent_cli", "claude not on PATH")]
    assert runner.calls == [(_DOCTOR_ARGV, _CALL_TIMEOUT_S)]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(returncode=3, stdout="", stderr="Project state is missing"),
        CommandResult(returncode=0, stdout=json.dumps({"ok": True}), stderr=""),
    ],
)
def test_doctor_output_without_a_report_is_a_protocol_error(result: CommandResult) -> None:
    runner = FakeRunner(result)

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruProtocolError):
            await client.doctor()

    _run(FakeSession(), scenario, runner)


@pytest.mark.parametrize("missing", ["result", "message"])
def test_an_ack_without_a_field_the_client_reads_is_a_protocol_error(missing: str) -> None:
    ack = {"id": 3, "status": "done", "result": None, "message": None}
    del ack[missing]
    session = FakeSession({"resolve_escalation": _reply(ack)})

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruProtocolError):
            await client.resolve_escalation("E-1", "retry")

    _run(session, scenario)


def _probe_session() -> FakeSession:
    return FakeSession(
        {
            "status": _reply(_recorded("status")),
            "list_escalations": _reply(_recorded("list_escalations")),
            "list_tasks": _reply(_recorded("list_tasks")),
        }
    )


def test_a_link_offering_every_declared_tool_and_answering_the_reads_passes_the_probe() -> None:
    asyncio.run(probe_link(FakeLink([_probe_session()]), _CALL_TIMEOUT_S))


@pytest.mark.parametrize("absent", sorted(DECLARED_TOOLS))
def test_a_link_that_does_not_offer_a_declared_tool_fails_the_probe(absent: str) -> None:
    session = _probe_session()
    session.offered = DECLARED_TOOLS - {absent}

    async def scenario() -> None:
        with pytest.raises(ShikiruProtocolError) as raised:
            await probe_link(FakeLink([session]), _CALL_TIMEOUT_S)
        assert absent in str(raised.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["status", "list_escalations", "list_tasks"])
def test_a_probe_answer_that_cannot_be_read_fails_the_probe(tool: str) -> None:
    session = _probe_session()
    session.replies[tool] = _reply({"unexpected": True})

    async def scenario() -> None:
        with pytest.raises(ShikiruProtocolError):
            await probe_link(FakeLink([session]), _CALL_TIMEOUT_S)

    asyncio.run(scenario())


class _RefusingLink:
    """A link whose connection raises `error`, as a process that cannot start does."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __call__(self) -> Any:
        return self

    async def __aenter__(self) -> ToolSession:
        raise self._error

    async def __aexit__(self, *_ignored: object) -> None: ...


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OSError("no such executable"), "no such executable"),
        (ExceptionGroup("unhandled errors", [OSError("no such executable")]), "no such executable"),
        (
            ExceptionGroup("unhandled errors", [OSError("first"), ValueError("second")]),
            "first; second",
        ),
        (ExceptionGroup("unhandled errors", [OSError("same"), OSError("same")]), "same"),
        (
            ExceptionGroup(
                "unhandled errors",
                [ExceptionGroup("nested", [OSError("deep")]), ValueError("top")],
            ),
            "deep; top",
        ),
    ],
)
def test_a_link_that_does_not_come_up_names_the_leaf_errors(
    error: BaseException, reason: str
) -> None:
    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError) as raised:
            await probe_link(_RefusingLink(error), _CALL_TIMEOUT_S)
        assert str(raised.value) == f"the link did not come up: {reason}"

    asyncio.run(scenario())


@pytest.mark.parametrize("state_directory", [None, SHIKIRU_STATE_DIRECTORY])
def test_a_path_is_connected_only_when_it_has_a_shikiru_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_directory: str | None
) -> None:
    if state_directory is not None:
        (tmp_path / state_directory).mkdir()
    attempts: list[StdioServerParameters] = []

    def stdio_client_stub(parameters: StdioServerParameters, *, errlog: TextIO) -> _RefusingLink:
        attempts.append(parameters)
        assert errlog.fileno() >= 0
        return _RefusingLink(OSError("no process"))

    monkeypatch.setattr("shikiri_yaku.adapters.shikiru_mcp.stdio_client", stdio_client_stub)

    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError) as raised:
            await probe_link(stdio_connector("shikiru", tmp_path), _CALL_TIMEOUT_S)
        named = f"{tmp_path} is not a Shikiru project"
        assert (named in str(raised.value)) is (state_directory is None)

    asyncio.run(scenario())
    assert bool(attempts) is (state_directory is not None)


_FAKE_MCP_SERVER = """#!{python}
import asyncio
import logging
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logging.getLogger("fake").warning("shikiru diagnostic kept")
routine = "INFO     Processing request of type ListToolsRequest"
logging.getLogger("fake").error("handler failed after %s", routine)
{extra}
server = Server("fake")

@server.list_tools()
async def list_tools():
    return []

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(main())
"""


def _write_fake_server(path: Path, extra: str = "") -> str:
    path.write_text(_FAKE_MCP_SERVER.format(python=sys.executable, extra=extra), encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_stdio_connector_drops_only_routine_info_traces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / SHIKIRU_STATE_DIRECTORY).mkdir()
    executable = _write_fake_server(tmp_path / "fake-mcp")

    async def scenario() -> None:
        async with stdio_connector(executable, tmp_path)() as session:
            await session.list_tools()

    asyncio.run(scenario())
    err = capsys.readouterr().err
    assert not any(line.startswith("INFO") for line in err.splitlines())
    assert "shikiru diagnostic kept" in err
    assert "handler failed after INFO     Processing request of type ListToolsRequest" in err


def test_stdio_connector_returns_when_a_child_keeps_stderr_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / SHIKIRU_STATE_DIRECTORY).mkdir()
    pid_file = tmp_path / "child.pid"
    extra = (
        "import subprocess\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
    )
    executable = _write_fake_server(tmp_path / "fake-mcp", extra=extra)
    monkeypatch.setattr("shikiri_yaku.adapters.shikiru_mcp.STDERR_DRAIN_S", 0.5)

    async def scenario() -> None:
        async with stdio_connector(executable, tmp_path)() as session:
            await session.list_tools()

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
