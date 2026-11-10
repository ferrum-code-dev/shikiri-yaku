from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE_WORKFLOW: Final = WORKFLOWS / "release.yml"
CREDENTIAL_CONTEXT: Final = "github.token"
JOB_ORDER: Final = ("prepare", "build", "pypi", "github")
NEEDS: Final = {
    "prepare": [],
    "build": ["prepare"],
    "pypi": ["build"],
    "github": ["prepare", "pypi"],
}
PERMISSIONS: Final = {
    "prepare": {"contents": "read", "actions": "read"},
    "build": {"contents": "read"},
    "pypi": {"id-token": "write"},
    "github": {"contents": "write"},
}
PYPI_VERSION_API: Final = "https://pypi.org/pypi/shikiri-yaku/"
TOKEN_STEPS: Final = frozenset(
    {
        ("prepare", "Find the newest commit of main that passed CI"),
        ("github", "Create the GitHub release"),
    },
)


def _load() -> dict[object, object]:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    return cast("dict[object, object]", yaml.safe_load(text))


def _jobs() -> dict[str, dict[str, object]]:
    return cast("dict[str, dict[str, object]]", _load()["jobs"])


def _needs(job: dict[str, object]) -> list[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return [value]
    return cast("list[str]", value)


def _mentions_token(node: object) -> bool:
    return CREDENTIAL_CONTEXT in json.dumps(node)


def test_the_release_starts_only_from_the_button_with_an_optional_version() -> None:
    # keep: PyYAML reads the key `on` as the boolean True
    trigger = cast("dict[str, object]", _load()[True])
    dispatch = cast("dict[str, object]", trigger["workflow_dispatch"])
    inputs = cast("dict[str, dict[str, object]]", dispatch["inputs"])

    assert set(trigger) == {"workflow_dispatch"}
    assert set(inputs) == {"version"}
    assert inputs["version"]["type"] == "string"
    assert inputs["version"]["required"] is False


def test_the_workflow_grants_no_permissions_and_one_release_at_a_time() -> None:
    workflow = _load()

    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {"group": "release", "cancel-in-progress": False}


def test_the_jobs_run_as_prepare_build_pypi_github() -> None:
    jobs = _jobs()

    assert tuple(jobs) == JOB_ORDER
    assert {name: _needs(job) for name, job in jobs.items()} == NEEDS
    assert {name: job.get("permissions") for name, job in jobs.items()} == PERMISSIONS


def test_prepare_refuses_a_version_pypi_already_has() -> None:
    steps = cast("list[dict[str, object]]", _jobs()["prepare"]["steps"])

    assert any(PYPI_VERSION_API in str(step.get("run", "")) for step in steps)


def test_publishing_still_waits_for_the_pypi_environment() -> None:
    assert _jobs()["pypi"]["environment"] == "pypi"


def test_only_the_ci_lookup_and_the_release_step_see_a_token() -> None:
    seen: set[tuple[str, str]] = set()
    for job_name, job in _jobs().items():
        job_level = {key: value for key, value in job.items() if key != "steps"}
        assert not _mentions_token(job_level), job_name
        for step in cast("list[dict[str, object]]", job["steps"]):
            if _mentions_token(step):
                seen.add((job_name, str(step["name"])))

    assert seen == TOKEN_STEPS
