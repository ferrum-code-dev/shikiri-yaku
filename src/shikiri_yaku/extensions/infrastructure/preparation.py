"""Read-only preparation of one model-proposed host-tool install (design §6.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.recipes import InstallPlan, Refusal, validate_install

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import InstallFacts, InstallProposalLog, InstallToolCheck


class InstallPreparation:
    """Validate the stored proposal and confirm its tool is absent, without journaling an op."""

    def __init__(
        self,
        *,
        proposals: InstallProposalLog,
        facts: InstallFacts,
        check: InstallToolCheck,
    ) -> None:
        self._proposals = proposals
        self._facts = facts
        self._check = check

    async def prepare(self, signal: Signal) -> InstallPlan | Forward:
        proposal = self._proposals.install_proposal(signal.id)
        if proposal is None:
            return Forward("host-tool classification has no complete stored proposal")
        if not proposal.tool or not proposal.manager or not proposal.package or not proposal.why:
            return Forward("host-tool classification has no complete stored proposal")

        host = await self._facts.collect()
        plan = validate_install(
            proposal.tool,
            proposal.manager,
            proposal.package,
            host.managers,
        )
        if isinstance(plan, Refusal):
            return Forward(f"install proposal refused: {plan.reason}")

        found = await self._check.find(plan.tool)
        if found is not None:
            return Forward(f"{plan.tool} already exists at {found}")
        return plan
