from __future__ import annotations

import re
from pathlib import Path
from typing import Final, cast

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CI: Final = WORKFLOWS / "ci.yml"
RELEASE: Final = WORKFLOWS / "release.yml"
WORKFLOW_FILE: Final = re.compile(r"[\w.-]+\.yml")
SETUP_UV: Final = "astral-sh/setup-uv@"
CHECKOUT: Final = "actions/checkout@"
CONTRACT_CHECK: Final = (
    "uv run python .github/scripts/contract_check.py --shikiru-checkout shikiru-src"
)


def _jobs(path: Path) -> dict[str, dict[str, object]]:
    document = cast("dict[str, object]", yaml.safe_load(path.read_text(encoding="utf-8")))
    return cast("dict[str, dict[str, object]]", document["jobs"])


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", job["steps"])


def _uses(job: dict[str, object], action: str) -> list[dict[str, object]]:
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(action)]


def _ci_document() -> dict[object, object]:
    return cast("dict[object, object]", yaml.safe_load(CI.read_text(encoding="utf-8")))


def _trigger() -> dict[str, object]:
    # keep: PyYAML reads the key `on` as the boolean True
    return cast("dict[str, object]", _ci_document()[True])


def test_the_contract_is_a_job_of_ci_on_every_push_to_main_and_every_pull_request() -> None:
    trigger = _trigger()

    assert set(trigger) == {"push", "pull_request", "schedule"}
    assert trigger["push"] == {"branches": ["main"]}
    assert _jobs(CI)["contract"]["name"] == "Shikiru contract"


def test_the_contract_also_runs_every_day_at_0100_utc() -> None:
    assert _trigger()["schedule"] == [{"cron": "0 1 * * *"}]


def test_a_scheduled_run_runs_the_contract_job_alone() -> None:
    jobs = _jobs(CI)

    assert jobs["check"]["if"] == "github.event_name != 'schedule'"
    assert "if" not in jobs["contract"]


def test_a_scheduled_run_has_a_concurrency_group_of_its_own() -> None:
    concurrency = cast("dict[str, object]", _ci_document()["concurrency"])

    assert concurrency == {
        "group": (
            "${{ github.workflow }}-"
            "${{ github.event_name == 'schedule' && 'schedule' || github.ref }}"
        ),
        "cancel-in-progress": True,
    }


def test_the_release_takes_its_commit_from_ci_alone() -> None:
    text = RELEASE.read_text(encoding="utf-8")

    assert WORKFLOW_FILE.findall(text) == ["ci.yml"]


def test_the_contract_job_sets_uv_up_exactly_as_the_check_job_does() -> None:
    jobs = _jobs(CI)
    (check_setup,) = _uses(jobs["check"], SETUP_UV)
    (contract_setup,) = _uses(jobs["contract"], SETUP_UV)

    assert contract_setup == check_setup


def test_the_contract_job_runs_on_macos_and_takes_tmux_from_homebrew_without_sudo() -> None:
    contract = _jobs(CI)["contract"]
    runs = [str(step["run"]) for step in _steps(contract) if "run" in step]

    assert contract["runs-on"] == "macos-15"
    assert any(run == "brew install tmux" for run in runs)
    assert not any("sudo" in run or "apt" in run for run in runs)


def test_the_contract_check_runs_against_the_shikiru_checkout_it_made() -> None:
    contract = _jobs(CI)["contract"]
    (shikiru,) = [
        step
        for step in _uses(contract, CHECKOUT)
        if cast("dict[str, object]", step.get("with", {})).get("path") == "shikiru-src"
    ]

    assert cast("dict[str, object]", shikiru["with"])["persist-credentials"] is False
    assert _steps(contract)[-1]["run"] == CONTRACT_CHECK
