"""The product extension: the category no other rule claims."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.domain.classification import Category
from shikiri_yaku.domain.extension import Extension, Rule
from shikiri_yaku.extensions.product.handler import ProductHandler

PRODUCT: Final = Category("product")


def product_extension() -> Extension:
    """Return the product category; its catch-all rule must be the last one registered."""
    return Extension(PRODUCT, (Rule(catch_all=True),), ProductHandler())
