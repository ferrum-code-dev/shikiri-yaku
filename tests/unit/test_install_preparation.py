from __future__ import annotations

import asyncio

import pytest

from shikiri_yaku.domain.classification import InstallProposal
from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.recipes import InstallPlan
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.preparation import InstallPreparation
from shikiri_yaku.ports import HostObservation

SIGNAL = Signal(
    project="api",
    kind=Kind.ESCALATION,
    source_id="E-1",
    reason="decision_required",
    text="the build needs ffmpeg",
    task_id="T-1",
    escalation_id="E-1",
)


class Proposals:
    def __init__(self, proposal: InstallProposal | None) -> None:
        self.proposal = proposal

    def install_proposal(self, signal_id: str) -> InstallProposal | None:
        assert signal_id == SIGNAL.id
        return self.proposal


class Facts:
    def __init__(self, managers: frozenset[str]) -> None:
        self.managers = managers
        self.calls = 0

    async def collect(self) -> HostObservation:
        self.calls += 1
        return HostObservation(("OS: test",), self.managers)


class ToolCheck:
    def __init__(self, found: str | None = None) -> None:
        self.found = found
        self.tools: list[str] = []

    async def find(self, tool: str) -> str | None:
        self.tools.append(tool)
        return self.found


def _proposal(
    *,
    tool: str | None = "ffmpeg",
    manager: str | None = "brew",
    package: str | None = "ffmpeg",
    why: str | None = "needed by the build",
) -> InstallProposal:
    return InstallProposal(tool=tool, manager=manager, package=package, why=why)


def _preparation(
    proposal: InstallProposal | None,
    *,
    managers: frozenset[str] = frozenset({"brew"}),
    found: str | None = None,
) -> tuple[InstallPreparation, Facts, ToolCheck]:
    facts = Facts(managers)
    check = ToolCheck(found)
    return (
        InstallPreparation(proposals=Proposals(proposal), facts=facts, check=check),
        facts,
        check,
    )


def test_preparation_returns_the_validated_plan_after_checking_for_the_tool() -> None:
    preparation, facts, check = _preparation(_proposal())

    result = asyncio.run(preparation.prepare(SIGNAL))

    assert result == InstallPlan("ffmpeg", "brew", "ffmpeg", ("brew", "install", "ffmpeg"))
    assert facts.calls == 1
    assert check.tools == ["ffmpeg"]


@pytest.mark.parametrize(
    ("proposal", "managers", "reason"),
    [
        (_proposal(package="ffmpeg;echo unsafe"), frozenset({"brew"}), "bad_package"),
        (_proposal(manager="apt"), frozenset({"brew"}), "manager_not_allowed"),
        (_proposal(manager="uv"), frozenset({"brew"}), "manager_missing"),
        (_proposal(tool="bad/name"), frozenset({"brew"}), "bad_tool"),
    ],
)
def test_refused_proposals_never_reach_tool_check(
    proposal: InstallProposal, managers: frozenset[str], reason: str
) -> None:
    preparation, facts, check = _preparation(proposal, managers=managers)

    result = asyncio.run(preparation.prepare(SIGNAL))

    assert isinstance(result, Forward)
    assert reason in result.note
    assert facts.calls == 1
    assert check.tools == []


@pytest.mark.parametrize(
    "proposal",
    [
        None,
        _proposal(tool=None),
        _proposal(manager=None),
        _proposal(package=None),
        _proposal(why=None),
    ],
    ids=["proposal", "tool", "manager", "package", "why"],
)
def test_incomplete_proposal_is_forwarded_without_host_probes(
    proposal: InstallProposal | None,
) -> None:
    preparation, facts, check = _preparation(proposal)

    result = asyncio.run(preparation.prepare(SIGNAL))

    assert isinstance(result, Forward)
    assert "no complete stored proposal" in result.note
    assert facts.calls == 0
    assert check.tools == []


def test_existing_tool_is_forwarded_with_its_path() -> None:
    preparation, facts, check = _preparation(_proposal(), found="/opt/homebrew/bin/ffmpeg")

    result = asyncio.run(preparation.prepare(SIGNAL))

    assert isinstance(result, Forward)
    assert "already exists at /opt/homebrew/bin/ffmpeg" in result.note
    assert facts.calls == 1
    assert check.tools == ["ffmpeg"]
