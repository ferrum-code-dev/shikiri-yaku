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


def test_the_contract_is_a_job_of_ci_on_every_push_to_main_and_every_pull_request() -> None:
    document = cast("dict[object, object]", yaml.safe_load(CI.read_text(encoding="utf-8")))
    # keep: PyYAML reads the key `on` as the boolean True
    trigger = cast("dict[str, object]", document[True])

    assert set(trigger) == {"push", "pull_request"}
    assert trigger["push"] == {"branches": ["main"]}
    assert _jobs(CI)["contract"]["name"] == "Shikiru contract"


def test_the_release_takes_its_commit_from_ci_alone() -> None:
    text = RELEASE.read_text(encoding="utf-8")

    assert WORKFLOW_FILE.findall(text) == ["ci.yml"]


def test_the_contract_job_sets_uv_up_exactly_as_the_check_job_does() -> None:
    jobs = _jobs(CI)
    (check_setup,) = _uses(jobs["check"], SETUP_UV)
    (contract_setup,) = _uses(jobs["contract"], SETUP_UV)

    assert contract_setup == check_setup


def test_the_contract_check_runs_against_the_shikiru_checkout_it_made() -> None:
    contract = _jobs(CI)["contract"]
    (shikiru,) = [
        step
        for step in _uses(contract, CHECKOUT)
        if cast("dict[str, object]", step.get("with", {})).get("path") == "shikiru-src"
    ]

    assert cast("dict[str, object]", shikiru["with"])["persist-credentials"] is False
    assert _steps(contract)[-1]["run"] == CONTRACT_CHECK
