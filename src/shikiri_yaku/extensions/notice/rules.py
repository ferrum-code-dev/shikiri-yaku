"""The rule of the notice category: the information the PO gets and does not decide (design §5)."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import Rule
from shikiri_yaku.domain.signal import Kind

NOTICE: Final = Category("notice")

NOTICE_RULE: Final = Rule(kinds=frozenset({Kind.TASK_DONE, Kind.HEALTH}))
