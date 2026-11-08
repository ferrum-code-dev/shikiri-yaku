"""Waits for the CI run of a commit to pass, before the release workflow publishes it.

`python scripts/wait_for_ci.py <sha> --repo <owner/name> --workflow <file>` lists the runs of that
workflow on the commit with `gh run list` every `POLL_S` seconds. It exits 0 when a run succeeded.
It exits 1 with a message when the runs failed, when no run started in `START_WAIT_S` seconds, or
when the runs did not finish within `TOTAL_WAIT_S` seconds. The decision is a pure function of the
runs and the elapsed time. The runner and the pause are passed in, so the tests call neither `gh`
nor `sleep`. The script uses the standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

POLL_S: Final = 30
START_WAIT_S: Final = 300
TOTAL_WAIT_S: Final = 1500
COMPLETED: Final = "completed"
SUCCESS: Final = "success"
FAILED: Final = 1


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Run:
    status: str
    conclusion: str


class Outcome(StrEnum):
    WAIT = "wait"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    message: str = ""


class ListError(Exception):
    """`gh run list` did not answer, so there is no verdict to give."""


type Runner = Callable[[Sequence[str]], CommandResult]
type Pause = Callable[[float], None]


def list_argv(repo: str, workflow: str, sha: str) -> tuple[str, ...]:
    return (
        "gh",
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--commit",
        sha,
        "--json",
        "status,conclusion",
    )


def decide(runs: Sequence[Run], elapsed_s: float) -> Verdict:
    """Return the verdict for the runs seen so far.

    A completed success wins over any failure, because a re-run of a failed run is the same commit.
    A run that is not completed has no conclusion to trust yet, so it never passes, and while one is
    in progress the commit is still waiting; only a commit with no run left in progress can fail on
    its conclusions.
    """
    if any(run.status == COMPLETED and run.conclusion == SUCCESS for run in runs):
        return Verdict(Outcome.PASSED)
    if any(run.status != COMPLETED for run in runs):
        if elapsed_s >= TOTAL_WAIT_S:
            return Verdict(Outcome.FAILED, f"CI did not finish within {TOTAL_WAIT_S} seconds")
        return Verdict(Outcome.WAIT)
    if runs:
        conclusions = ", ".join(sorted({run.conclusion for run in runs}))
        return Verdict(Outcome.FAILED, f"CI ended without a success: {conclusions}")
    if elapsed_s >= START_WAIT_S:
        return Verdict(Outcome.FAILED, "CI did not start for this commit")
    return Verdict(Outcome.WAIT)


def parse_runs(stdout: str) -> list[Run]:
    return [Run(str(entry["status"]), str(entry["conclusion"])) for entry in json.loads(stdout)]


def list_runs(run: Runner, repo: str, workflow: str, sha: str) -> list[Run]:
    result = run(list_argv(repo, workflow, sha))
    if result.returncode != 0:
        message = f"gh run list failed:\n{result.stderr}"
        raise ListError(message)
    return parse_runs(result.stdout)


def wait_for_verdict(run: Runner, pause: Pause, repo: str, workflow: str, sha: str) -> Verdict:
    elapsed_s = 0.0
    while True:
        verdict = decide(list_runs(run, repo, workflow, sha), elapsed_s)
        if verdict.outcome is not Outcome.WAIT:
            return verdict
        pause(POLL_S)
        elapsed_s += POLL_S


def run_gh(argv: Sequence[str]) -> CommandResult:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        return CommandResult(FAILED, "", str(error))
    return CommandResult(done.returncode, done.stdout, done.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for the CI run of a commit to pass.")
    parser.add_argument("sha")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", required=True)
    args = parser.parse_args(argv)
    try:
        verdict = wait_for_verdict(run_gh, time.sleep, args.repo, args.workflow, args.sha)
    except (ListError, ValueError, KeyError) as error:
        sys.stderr.write(f"wait_for_ci: {error}\n")
        return FAILED
    if verdict.outcome is Outcome.PASSED:
        sys.stdout.write(f"CI passed for {args.sha}\n")
        return 0
    sys.stderr.write(f"wait_for_ci: CI for {args.sha} is not green: {verdict.message}\n")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
