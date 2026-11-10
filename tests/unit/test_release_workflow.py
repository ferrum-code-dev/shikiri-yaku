from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

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
    "pypi": ["prepare", "build"],
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


def _uses(job: dict[str, object], action: str) -> list[dict[str, object]]:
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(action)]


def _step_named(job: dict[str, object], name: str) -> dict[str, object]:
    (step,) = [step for step in _steps(job) if step.get("name") == name]
    return step


def _index_of(job: dict[str, object], predicate: Callable[[dict[str, object]], bool]) -> int:
    (index,) = [index for index, step in enumerate(_steps(job)) if predicate(step)]
    return index


def test_the_release_runs_daily_and_on_the_button_with_an_optional_version() -> None:
    # keep: PyYAML reads the key `on` as the boolean True
    trigger = cast("dict[str, object]", _load()[True])
    dispatch = cast("dict[str, object]", trigger["workflow_dispatch"])
    inputs = cast("dict[str, dict[str, object]]", dispatch["inputs"])
    schedule = cast("list[dict[str, str]]", trigger["schedule"])

    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert schedule == [{"cron": "0 0 * * *"}]
    assert set(inputs) == {"version"}
    assert inputs["version"]["type"] == "string"
    assert inputs["version"]["required"] is False


def test_a_scheduled_ci_run_never_decides_the_release() -> None:
    # keep: the daily 01:00 UTC run of ci.yml is a schedule, and only a push may choose the release
    lookup = str(
        _step_named(_jobs()["prepare"], "Find the newest commit of main that passed CI")["run"]
    )

    assert "--workflow ci.yml --branch main --event push" in lookup


def test_the_build_pypi_and_github_jobs_run_only_when_prepare_finds_a_release() -> None:
    jobs = _jobs()

    assert "release" in cast("dict[str, object]", jobs["prepare"]["outputs"])
    for name in ("build", "pypi", "github"):
        assert jobs[name]["if"] == "needs.prepare.outputs.release == 'true'", name
    assert "if" not in jobs["prepare"]


def test_the_github_release_is_marked_latest_and_never_a_pre_release() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "--latest" in text
    assert "--prerelease" not in text


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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """main has tag v0.1.0 and one commit after it; an orphan commit is not after the tag."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: first")
    _git(tmp_path, "tag", "v0.1.0")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: second")
    after = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "--orphan", "elsewhere")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: elsewhere")
    return {"after": after, "elsewhere": _git(tmp_path, "rev-parse", "HEAD")}


def _choose(repo: Path, *, sha: str, requested: str = "", proposed: str = "") -> dict[str, str]:
    """Run the Choose the version step's script as the runner does, and read its outputs."""
    script = str(_step_named(_jobs()["prepare"], "Choose the version")["run"])
    outputs = repo / "github-output"
    env = {
        "PATH": os.environ["PATH"],
        "SHA": sha,
        "REQUESTED": requested,
        "PROPOSED": proposed,
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(repo / "github-summary"),
    }
    subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize(
    ("commit", "requested", "proposed", "version", "release"),
    [
        pytest.param("after", "", "v0.2.0", "0.2.0", "true", id="proposal-after-the-last-tag"),
        pytest.param("after", "", "v0.1.0", "0.1.0", "false", id="proposal-is-the-last-tag"),
        pytest.param("elsewhere", "", "v0.2.0", "0.2.0", "false", id="proposal-not-after-the-tag"),
        pytest.param("after", "0.2.0", "", "0.2.0", "true", id="requested-after-the-last-tag"),
        pytest.param("elsewhere", "0.2.0", "", "0.2.0", "false", id="requested-not-after-the-tag"),
    ],
)
def test_only_a_commit_after_the_last_release_tag_is_released(
    tmp_path: Path,
    history: dict[str, str],
    *,
    commit: str,
    requested: str,
    proposed: str,
    version: str,
    release: str,
) -> None:
    outputs = _choose(tmp_path, sha=history[commit], requested=requested, proposed=proposed)

    assert outputs == {"version": version, "release": release}


def test_the_pypi_upload_skips_files_pypi_already_has() -> None:
    (publish,) = _uses(_jobs()["pypi"], "pypa/gh-action-pypi-publish@")

    assert publish["with"] == {"skip-existing": True}


def test_the_github_release_takes_no_files_from_the_build() -> None:
    github = _jobs()["github"]
    create = _step_named(github, "Create the GitHub release")

    assert not _uses(github, "actions/download-artifact@")
    assert "dist" not in str(create["run"])


def test_the_installed_wheel_is_run_and_must_report_the_version_before_upload() -> None:
    build = _jobs()["build"]
    check = _index_of(
        build, lambda step: "--isolated --no-project --with" in str(step.get("run", ""))
    )
    upload = _index_of(
        build, lambda step: str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )

    assert check < upload


def test_only_the_ci_lookup_and_the_release_step_see_a_token() -> None:
    seen: set[tuple[str, str]] = set()
    for job_name, job in _jobs().items():
        job_level = {key: value for key, value in job.items() if key != "steps"}
        assert not _mentions_token(job_level), job_name
        for step in cast("list[dict[str, object]]", job["steps"]):
            if _mentions_token(step):
                seen.add((job_name, str(step["name"])))

    assert seen == TOKEN_STEPS
