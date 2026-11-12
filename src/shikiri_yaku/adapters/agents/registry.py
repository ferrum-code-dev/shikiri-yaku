"""The agents this build runs. Adding one is a module and one line here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.adapters.agents import claude, codex, muse

if TYPE_CHECKING:
    from shikiri_yaku.adapters.agents.common import AgentAdapter

AGENTS: Final[dict[str, AgentAdapter]] = {
    agent.name: agent
    for agent in (
        claude.AGENT,
        codex.AGENT,
        muse.AGENT,
    )
}
