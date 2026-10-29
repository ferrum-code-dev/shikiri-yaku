"""The rule of the infrastructure category: the escalations and kinds it sorts here (design §5)."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import Rule
from shikiri_yaku.domain.signal import Kind

INFRASTRUCTURE: Final = Category("infrastructure")

AGENT_ERROR: Final = "agent_error"
AGENT_STUCK: Final = "agent_stuck"
AGENT_PROTOCOL: Final = "agent_protocol"
INTEGRATION_CONFLICT: Final = "integration_conflict"
INTEGRATION_BLOCKED: Final = "integration_blocked"
QUOTA_EXHAUSTED: Final = "quota_exhausted"
LADDER_EXHAUSTED: Final = "ladder_exhausted"
ENGINE_ERROR: Final = "engine_error"

REASONS: Final = frozenset(
    {
        AGENT_ERROR,
        AGENT_STUCK,
        AGENT_PROTOCOL,
        INTEGRATION_CONFLICT,
        INTEGRATION_BLOCKED,
        QUOTA_EXHAUSTED,
        LADDER_EXHAUSTED,
        ENGINE_ERROR,
    }
)
KINDS: Final = frozenset({Kind.ENGINE_DOWN, Kind.ENGINE_FAULT})

INFRASTRUCTURE_RULE: Final = Rule(reasons=REASONS, kinds=KINDS)
