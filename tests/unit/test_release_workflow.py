from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final, cast

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE_WORKFLOW: Final = WORKFLOWS / "release.yml"
CREDENTIAL_CONTEXT: Final = "github.token"
GIT_CLIFF_ACTION: Final = "orhun/git-cliff-action@"
GIT_CLIFF_VERSION: Final = "v2.14.2"
NEEDS_OUTPUT: Final = re.compile(r"needs\.(\w+)\.outputs\.(\w+)")
STEP_OUTPUT: Final = re.compile(r"steps\.([\w-]+)\.outputs\.(\w+)")
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


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", job["steps"])


def _mentions_token(node: object) -> bool:
    return CREDENTIAL_CONTEXT in json.dumps(node)


def _git_cliff_uses(job: dict[str, object]) -> list[dict[str, object]]:
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(GIT_CLIFF_ACTION)]


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


def test_git_cliff_proposes_the_version_in_prepare_and_writes_the_notes_in_github() -> None:
    jobs = _jobs()

    proposal = [step["with"] for step in _git_cliff_uses(jobs["prepare"])]
    notes = [step["with"] for step in _git_cliff_uses(jobs["github"])]

    assert proposal == [{"version": GIT_CLIFF_VERSION, "args": "--bumped-version"}]
    assert notes == [
        {
            "version": GIT_CLIFF_VERSION,
            "args": "--unreleased --tag v${{ needs.prepare.outputs.version }} --strip all",
        },
    ]
    assert all(not _git_cliff_uses(jobs[name]) for name in ("build", "pypi"))


def test_no_release_step_runs_a_script_from_the_repository() -> None:
    for job in _jobs().values():
        for step in _steps(job):
            assert "scripts/" not in str(step.get("run", "")), step.get("name")


def test_the_outputs_a_job_reads_exist() -> None:
    jobs = _jobs()
    for name, job in jobs.items():
        text = json.dumps(job)
        step_ids = {str(step["id"]) for step in _steps(job) if "id" in step}
        for upstream, output in NEEDS_OUTPUT.findall(text):
            assert upstream in _needs(job), (name, upstream)
            assert output in cast("dict[str, object]", jobs[upstream].get("outputs", {})), (
                name,
                upstream,
                output,
            )
        for step_id, _ in STEP_OUTPUT.findall(text):
            assert step_id in step_ids, (name, step_id)


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
