"""One sample of each secret form the outbound scan refuses, and the messages that carry them."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.ports import Button, Message

SECRETS: Final = {
    "bot_token": "123456789:" + "A" * 35,
    "sk_key": "sk-" + "a" * 20,
    "pem_private_key": "-----BEGIN PRIVATE KEY-----",
    "github_token": "ghp_" + "a" * 36,
    "aws_access_key_id": "AKIA" + "A" * 16,
}
# keep: a suffix that ends in '-' is the case a trailing word boundary let through
HYPHEN_ENDED_TOKEN: Final = "123456789:" + "A" * 34 + "-"
# keep: what can follow a secret in text: nothing, a space, a separator, a quote or a line end
TAILS: Final = ("", " ", ",", ".", ")", '"', "\n")


def _carriers(secret: str) -> list[Message]:
    """The places a secret can sit in a message: its text, a button label and its callback data."""
    return [
        Message(f"S1 · {secret}"),
        Message("S1", heading=secret),
        Message("S1", (Button(secret, "a:1:1"),)),
        Message("S1", (Button("1", secret),)),
    ]


def secret_cases() -> list[tuple[str, str, Message]]:
    """Each form with its secret, in every place a message can carry it: (form, secret, message)."""
    cases = list(SECRETS.items())
    cases += [("bot_token", HYPHEN_ENDED_TOKEN + tail) for tail in TAILS]
    return [(form, secret, message) for form, secret in cases for message in _carriers(secret)]


def secret_messages() -> list[tuple[str, Message]]:
    """Each form and a message that carries it, for tests that need no copy of the secret."""
    return [(form, message) for form, _, message in secret_cases()]
