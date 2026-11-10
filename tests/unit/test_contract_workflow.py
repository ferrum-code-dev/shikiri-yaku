from __future__ import annotations

from pathlib import Path
from typing import Final, cast

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CONTRACT: Final = WORKFLOWS / "contract.yml"
CI: Final = WORKFLOWS / "ci.yml"
RELEASE: Final = WORKFLOWS / "release.yml"
SETUP_UV: Final = "astral-sh/setup-uv@"
CHECKOUT: Final = "actions/checkout@"


def _steps(path: Path) -> list[dict[str, object]]:
    document = cast("dict[str, object]", yaml.safe_load(path.read_text(encoding="utf-8")))
    jobs = cast("dict[str, dict[str, object]]", document["jobs"])
    return [step for job in jobs.values() for step in cast("list[dict[str, object]]", job["steps"])]


def _uses(path: Path, action: str) -> list[dict[str, object]]:
    return [step for step in _steps(path) if str(step.get("uses", "")).startswith(action)]


def test_the_contract_runs_on_every_change_daily_and_by_hand() -> None:
    document = cast("dict[object, object]", yaml.safe_load(CONTRACT.read_text(encoding="utf-8")))
    # keep: PyYAML reads the key `on` as the boolean True
    trigger = cast("dict[str, object]", document[True])

    assert set(trigger) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert trigger["push"] == {"branches": ["main"]}
    assert trigger["schedule"] == [{"cron": "0 1 * * *"}]


def test_the_release_does_not_wait_for_the_contract() -> None:
    text = RELEASE.read_text(encoding="utf-8")

    assert "ci.yml" in text
    assert "contract.yml" not in text


def test_the_contract_check_sets_uv_up_exactly_as_ci_does() -> None:
    (contract_setup,) = _uses(CONTRACT, SETUP_UV)
    (ci_setup,) = _uses(CI, SETUP_UV)

    assert contract_setup == ci_setup


def test_the_contract_check_runs_against_the_shikiru_checkout_it_made() -> None:
    (shikiru,) = [
        step
        for step in _uses(CONTRACT, CHECKOUT)
        if cast("dict[str, object]", step.get("with", {})).get("path") == "shikiru-src"
    ]
    last = _steps(CONTRACT)[-1]

    assert cast("dict[str, object]", shikiru["with"])["persist-credentials"] is False
    assert last["run"] == "uv run python scripts/contract_check.py --shikiru-checkout shikiru-src"
