"""The forms of secret that must never travel through chat.

A message that carries one of these is refused and its body is never kept. The list covers the
first release's forms and is not exhaustive. A form is named by its key here; the key is the only
thing the journal and the stderr line keep about it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

# keep: a trailing \b fails after '-', so a token that ends in '-' is missed
# keep: a leading \b fails after '_', which a token may follow, so no form takes one
# keep: sk- may not follow a letter or digit, so "desk-" in ordinary text is not taken for a key
FORMS: Final[dict[str, re.Pattern[str]]] = {
    "bot_token": re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
    "sk_key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "pem_private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    "aws_access_key_id": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
}


def secret_form(texts: Iterable[str]) -> str | None:
    """Return the key of the first secret form in any of the texts, or None."""
    for text in texts:
        for key, pattern in FORMS.items():
            if pattern.search(text):
                return key
    return None
