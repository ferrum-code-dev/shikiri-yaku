"""The rule of the notice category: the information the PO gets and does not decide (design §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import LegacyCopy, Rule
from shikiri_yaku.domain.outbound import FORWARD_KEY
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.notice.handler import HEALTH_KEY

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal

NOTICE: Final = Category("notice")

NOTICE_RULE: Final = Rule(kinds=frozenset({Kind.TASK_DONE, Kind.HEALTH}))
LEGACY_COPIES: Final = (
    LegacyCopy(f"{HEALTH_KEY}:", frozenset({Kind.HEALTH})),
    LegacyCopy(f"{FORWARD_KEY}:", frozenset({Kind.HEALTH})),
)


def stands_on_failure(signal: Signal) -> bool:
    """Keep a health message on screen with its reminders when its handler fails (§6.5)."""
    return signal.kind is Kind.HEALTH
