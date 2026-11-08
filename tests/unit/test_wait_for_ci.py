from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest
from scripts import wait_for_ci
from scripts.wait_for_ci import Outcome, Run

if TYPE_CHECKING:
    from collections.abc import Sequence

SHA: Final = "9ec6819d2f0b4c1e8a7f3b5d6e9c0a1b2c3d4e5f"
REPO: Final = "ferrum-code-dev/shikiri-yaku"
WORKFLOW: Final = "ci.yml"
QUEUED: Final = Run("queued", "")
IN_PROGRESS: Final = Run("in_progress", "")
SUCCEEDED: Final = Run("completed", "success")
FAILED_RUN: Final = Run("completed", "failure")
CANCELLED: Final = Run("completed", "cancelled")


@pytest.mark.parametrize(
    ("runs", "elapsed_s", "expected"),
    [
        ([SUCCEEDED], 0, Outcome.PASSED),
        ([FAILED_RUN], 0, Outcome.FAILED),
        ([FAILED_RUN, QUEUED], wait_for_ci.POLL_S, Outcome.WAIT),
        ([], wait_for_ci.START_WAIT_S - wait_for_ci.POLL_S, Outcome.WAIT),
        ([], wait_for_ci.START_WAIT_S, Outcome.FAILED),
        ([IN_PROGRESS], wait_for_ci.POLL_S, Outcome.WAIT),
        ([IN_PROGRESS], wait_for_ci.TOTAL_WAIT_S, Outcome.FAILED),
        ([FAILED_RUN, SUCCEEDED], 0, Outcome.PASSED),
        ([SUCCEEDED, FAILED_RUN], 0, Outcome.PASSED),
        ([FAILED_RUN, CANCELLED], 0, Outcome.FAILED),
        ([Run("queued", "success")], 0, Outcome.WAIT),
        ([Run("in_progress", "success")], wait_for_ci.TOTAL_WAIT_S, Outcome.FAILED),
        ([FAILED_RUN, Run("in_progress", "success")], 0, Outcome.WAIT),
    ],
)
def test_the_verdict_for_the_runs_seen(
    runs: Sequence[Run],
    elapsed_s: float,
    expected: Outcome,
) -> None:
    assert wait_for_ci.decide(runs, elapsed_s).outcome is expected


def test_a_failure_names_the_conclusion_of_the_run() -> None:
    verdict = wait_for_ci.decide([CANCELLED], 0)

    assert "cancelled" in verdict.message


def test_the_runs_of_the_commit_are_listed_with_gh() -> None:
    argv = wait_for_ci.list_argv(REPO, WORKFLOW, SHA)

    assert argv[:4] == ("gh", "run", "list", "--repo")
    assert argv[argv.index("--commit") + 1] == SHA
    assert argv[argv.index("--workflow") + 1] == WORKFLOW


def test_the_wait_polls_until_a_run_passes() -> None:
    answers = [
        json.dumps([{"status": "in_progress", "conclusion": ""}]),
        json.dumps([{"status": "completed", "conclusion": "success"}]),
    ]
    pauses: list[float] = []

    def run(_argv: Sequence[str]) -> wait_for_ci.CommandResult:
        return wait_for_ci.CommandResult(0, answers.pop(0), "")

    verdict = wait_for_ci.wait_for_verdict(run, pauses.append, REPO, WORKFLOW, SHA)

    assert verdict.outcome is Outcome.PASSED
    assert pauses == [wait_for_ci.POLL_S]


def test_a_gh_that_fails_stops_the_wait() -> None:
    def run(_argv: Sequence[str]) -> wait_for_ci.CommandResult:
        return wait_for_ci.CommandResult(1, "", "not logged in")

    with pytest.raises(wait_for_ci.ListError):
        wait_for_ci.wait_for_verdict(run, lambda _seconds: None, REPO, WORKFLOW, SHA)
