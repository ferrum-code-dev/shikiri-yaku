"""One sample of each secret form the outbound scan refuses, and the messages that carry them."""

from __future__ import annotations

from typing import Final

from shikiri_yaku.ports import Button, Message

SECRETS: Final = {
    "telegram_bot_token": "123456789:" + "A" * 35,
    "sk_key": "sk-" + "a" * 20,
    "pem_private_key": "-----BEGIN PRIVATE KEY-----",
    "github_token": "ghp_" + "a" * 36,
    "aws_access_key_id": "AKIA" + "A" * 16,
}


def secret_cases() -> list[tuple[str, Message]]:
    """Each form in a message's text, in a button label, and in its callback data."""
    return [
        (form, message)
        for form, secret in SECRETS.items()
        for message in (
            Message(f"S1 · {secret}"),
            Message("S1", (Button(secret, "a:1:1"),)),
            Message("S1", (Button("1", secret),)),
        )
    ]
