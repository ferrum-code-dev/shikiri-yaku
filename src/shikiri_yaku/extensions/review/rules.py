"""The rule of the review category: the pull requests it sorts here (design §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import LegacyCopy, Rule
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.review.handler import NOTICE_KEY

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal

REVIEW: Final = Category("review")

REVIEW_RULE: Final = Rule(kinds=frozenset({Kind.PR_REVIEW}))
LEGACY_COPIES: Final = (LegacyCopy(f"{NOTICE_KEY}:", frozenset({Kind.PR_REVIEW})),)


def stands_on_failure(signal: Signal) -> bool:
    """Keep a pull request open when its review message fails, until the PO answers (§6.4)."""
    return signal.kind is Kind.PR_REVIEW
