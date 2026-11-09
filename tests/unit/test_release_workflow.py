from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE_WORKFLOW: Final = WORKFLOWS / "release.yml"
CREDENTIAL_CONTEXT: Final = "github.token"
TOKEN_STEPS: Final = frozenset(
    {
        ("check", "Check that CI passed on the tagged commit"),
        ("github", "Create the GitHub release"),
    },
)


def _load() -> dict[str, object]:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    return cast("dict[str, object]", yaml.safe_load(text))


def _mentions_token(node: object) -> bool:
    return CREDENTIAL_CONTEXT in json.dumps(node)


def test_the_release_workflow_parses_as_yaml_with_all_jobs() -> None:
    workflow = _load()

    assert set(cast("dict[str, object]", workflow["jobs"])) == {"check", "build", "pypi", "github"}


def test_only_the_ci_wait_and_the_release_step_see_a_token() -> None:
    jobs = cast("dict[str, dict[str, object]]", _load()["jobs"])
    seen: set[tuple[str, str]] = set()
    for job_name, job in jobs.items():
        job_level = {key: value for key, value in job.items() if key != "steps"}
        assert not _mentions_token(job_level), job_name
        for step in cast("list[dict[str, object]]", job["steps"]):
            if _mentions_token(step):
                seen.add((job_name, str(step["name"])))

    assert seen == TOKEN_STEPS
