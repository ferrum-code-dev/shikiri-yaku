"""Checks the Shikiru assumptions of design §11 against a real engine with its offline fake agent.

A human runs it; the quality gate does not. Needs git and tmux on PATH and a Shikiru checkout:

    uv run python scripts/contract_check.py --shikiru-checkout /path/to/shikiru

It builds a temporary project from the checkout's sample project, starts the engine through the
adapter, lets the fake agent escalate, resolves the escalation twice and prints one line per
assumption. Exit code 0 means every assumption held.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Final

from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.adapters.shikiru_mcp import ShikiruMcpClient, stdio_connector
from shikiri_yaku.ports import ResolutionKind, ShikiruRejectedError

TIMEOUT_S: Final = 120
POLL_S: Final = 1
COMMAND_TIMEOUT_S: Final = 60
BACKOFF_MAX_S: Final = 8
QUESTION: Final = "Which database should the sample use?"
FAKE_LADDER: Final = '[{agent="fake",model="fake-1"}]'
WRAPPER_MODE: Final = 0o755
EVENT_WAIT_S: Final = 5
LEADER_CONSULTS_BEFORE_ESCALATION: Final = 3


class CheckFailedError(Exception):
    pass


def _expect(condition: object, assumption: str) -> None:
    if not condition:
        raise CheckFailedError(assumption)
    sys.stdout.write(f"ok: {assumption}\n")


def _wrapper(directory: Path, checkout: Path) -> Path:
    """A `shikiru` that runs the checkout and its fake agent, on a private tmux socket."""
    real_tmux = shutil.which("tmux")
    if real_tmux is None:
        message = "tmux is not on PATH"
        raise CheckFailedError(message)
    shim = directory / "bin" / "tmux"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\nexec {shlex.quote(real_tmux)} -L contract-check -f /dev/null "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(WRAPPER_MODE)
    overrides = [
        "orchestration.poll_seconds=1",
        "orchestration.resources.max_cpu_pct=100",
        "orchestration.resources.min_free_ram_gb=0",
        f"roles.leader.ladder={FAKE_LADDER}",
        f"roles.implementer.ladder={FAKE_LADDER}",
        f"roles.helper.ladder={FAKE_LADDER}",
    ]
    asking = {"kind": "needs_decision", "question": QUESTION}
    script = json.dumps([asking] * (LEADER_CONSULTS_BEFORE_ESCALATION + 1) + [{"kind": "stall"}])
    set_arguments = " ".join(f"--set {shlex.quote(item)}" for item in overrides)
    wrapper = directory / "shikiru"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export PATH={shlex.quote(str(shim.parent))}:$PATH\n"
        "unset TMUX TMUX_PANE\n"
        "tmux has-session -t keepalive 2>/dev/null || tmux new-session -d -s keepalive\n"
        f"export SHIKIRU_FAKE_SCRIPT_IMPLEMENTER={shlex.quote(script)}\n"
        f"tmux set-environment -g SHIKIRU_FAKE_SCRIPT_IMPLEMENTER {shlex.quote(script)}\n"
        f"exec uv run --no-sync --project {shlex.quote(str(checkout))} shikiru "
        f'{set_arguments} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(WRAPPER_MODE | stat.S_IXUSR)
    return wrapper


async def _prepare(project: Path, checkout: Path, wrapper: Path) -> None:
    shutil.copytree(checkout / "tests" / "fixtures" / "sample-project", project)
    runner = ProcessCommandRunner()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=check",
            "-c",
            "user.email=check@example.com",
            "commit",
            "-qm",
            "x",
        ],
        [str(wrapper), "--project", str(project), "init", "--yes"],
    ):
        result = await runner.run(["env", "-C", str(project), *argv], timeout_s=COMMAND_TIMEOUT_S)
        if result.returncode != 0:
            message = f"{' '.join(argv)} failed: {result.stderr.strip()}"
            raise CheckFailedError(message)


async def _until(clock: SystemClock, condition, what: str) -> None:  # noqa: ANN001
    for _ in range(TIMEOUT_S // POLL_S):
        if await condition():
            return
        await clock.sleep(POLL_S)
    message = f"timed out waiting for {what}"
    raise CheckFailedError(message)


async def _check(wrapper: Path, project: Path) -> None:
    clock = SystemClock()
    runner = ProcessCommandRunner()
    client = ShikiruMcpClient(
        stdio_connector(str(wrapper), project),
        clock,
        backoff_initial_s=POLL_S,
        backoff_max_s=BACKOFF_MAX_S,
        call_timeout_s=COMMAND_TIMEOUT_S,
        runner=runner,
        start_argv=[str(wrapper), "--project", str(project), "start"],
        start_timeout_s=COMMAND_TIMEOUT_S,
        doctor_argv=[str(wrapper), "--project", str(project), "--json", "doctor"],
    )
    supervisor = asyncio.create_task(client.supervise())
    try:
        await _scenario(client, clock)
    finally:
        supervisor.cancel()
        await asyncio.wait({supervisor}, timeout=COMMAND_TIMEOUT_S)
        await runner.run(
            [str(wrapper), "--project", str(project), "stop", "--kill-session"],
            timeout_s=COMMAND_TIMEOUT_S,
        )
        await runner.run(
            [str(wrapper.parent / "bin" / "tmux"), "kill-server"], timeout_s=COMMAND_TIMEOUT_S
        )


async def _scenario(client: ShikiruMcpClient, clock: SystemClock) -> None:
    async def link_up() -> bool:
        try:
            await client.status()
        except Exception:  # noqa: BLE001 -- any failure means the link is not up yet
            return False
        return True

    await _until(clock, link_up, "the MCP link")
    await client.start_engine()
    _expect((await client.status()).engine_up, "start brings status.engine_up")

    submitted = await client.submit_task("Implement task A1 from tasks/plan.md")
    _expect(submitted.task_id, f"submit_task acknowledges with a task id ({submitted.task_id})")

    events = []
    escalations = []

    async def escalated() -> bool:
        since = events[-1].id if events else 0
        events.extend(await client.wait_for_events(since, EVENT_WAIT_S))
        escalations[:] = await client.list_escalations()
        return bool(escalations)

    await _until(clock, escalated, "an escalation")
    _expect(events, "wait_for_events delivers events")
    view = escalations[0]
    _expect(view.question and QUESTION in view.question, "the escalation has a question")
    _expect(
        isinstance(view.agent_options, list),
        f"the escalation has agent_options ({len(view.agent_options)}; the fake agent offers none)",
    )
    _expect(view.resolutions, "the escalation has resolutions")

    ack = await client.resolve_escalation(view.escalation.id, ResolutionKind.ANSWER, "SQLite")
    _expect(ack.status in {"done", "queued"}, f"a resolve is acknowledged ({ack.status})")

    async def resolved() -> bool:
        since = events[-1].id
        events.extend(await client.wait_for_events(since, EVENT_WAIT_S))
        return any(event.kind == "escalation_resolved" for event in events)

    await _until(clock, resolved, "escalation_resolved")
    event = next(event for event in events if event.kind == "escalation_resolved")
    _expect(event.data.get("command_id") == ack.id, "escalation_resolved carries the command id")

    try:
        await client.resolve_escalation(view.escalation.id, ResolutionKind.ANSWER, "again")
    except ShikiruRejectedError as error:
        rejection = str(error)
    else:
        rejection = ""
    _expect(rejection, f"a second resolve is rejected ({rejection})")


async def _main(checkout: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="contract-check-") as name:
        directory = Path(name)
        wrapper = _wrapper(directory, checkout)
        project = directory / "project"
        try:
            await _prepare(project, checkout, wrapper)
            await _check(wrapper, project)
        except CheckFailedError as error:
            sys.stdout.write(f"FAILED: {error}\n")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shikiru-checkout", type=Path, required=True)
    arguments = parser.parse_args()
    os.environ.pop("VIRTUAL_ENV", None)
    return asyncio.run(_main(arguments.shikiru_checkout.resolve()))


if __name__ == "__main__":
    sys.exit(main())
