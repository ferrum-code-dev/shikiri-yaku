"""The secret forms the outbound scan refuses (design §7)."""

from __future__ import annotations

import pytest

from shikiri_yaku.domain.secrets import secret_form


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("bot 123456789:" + "A" * 35, "telegram_bot_token"),
        ("key sk-" + "a" * 20, "sk_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "pem_private_key"),
        ("-----BEGIN PRIVATE KEY-----", "pem_private_key"),
        ("token ghp_" + "a" * 36, "github_token"),
        ("token github_pat_" + "a" * 22, "github_token"),
        ("id AKIA" + "A" * 16, "aws_access_key_id"),
    ],
)
def test_each_secret_form_is_found(text: str, form: str) -> None:
    assert secret_form([text]) == form


@pytest.mark.parametrize(
    "text",
    [
        "a count of 12345 and a time of 9:30",
        "sk-short",
        "-----BEGIN CERTIFICATE-----",
        "id AKIA" + "a" * 16,
    ],
)
def test_ordinary_text_is_not_refused(text: str) -> None:
    assert secret_form([text]) is None


def test_a_secret_in_any_of_the_texts_is_found() -> None:
    assert secret_form(["S1 · a reply", "sk-" + "a" * 20]) == "sk_key"
