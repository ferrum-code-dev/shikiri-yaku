"""The rule of the review category: the pull requests it sorts here (design §5)."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import Rule
from shikiri_yaku.domain.signal import Kind

REVIEW: Final = Category("review")

REVIEW_RULE: Final = Rule(kinds=frozenset({Kind.PR_REVIEW}))
