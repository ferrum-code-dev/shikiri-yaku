"""The secret forms the outbound scan refuses (design §7)."""

from __future__ import annotations

import pytest

from shikiri_yaku.domain.secrets import secret_form
from tests.support.secrets import HYPHEN_ENDED_TOKEN, TAILS


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("bot 123456789:" + "A" * 35, "bot_token"),
        ("key sk-" + "a" * 20, "sk_key"),
        ("key_sk-" + "a" * 20, "sk_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "pem_private_key"),
        ("-----BEGIN PRIVATE KEY-----", "pem_private_key"),
        ("token ghp_" + "a" * 36, "github_token"),
        ("token github_pat_" + "a" * 22, "github_token"),
        ("id AKIA" + "A" * 16, "aws_access_key_id"),
        ("id AKIA" + "A" * 16 + "_next", "aws_access_key_id"),
        ("key_AKIA" + "A" * 16, "aws_access_key_id"),
        ("token_ghp_" + "a" * 36, "github_token"),
        ("my_github_pat_" + "a" * 22, "github_token"),
    ],
)
def test_each_secret_form_is_found(text: str, form: str) -> None:
    assert secret_form([text]) == form


@pytest.mark.parametrize("tail", TAILS)
def test_a_token_whose_suffix_ends_in_a_hyphen_is_found_before_any_separator(tail: str) -> None:
    assert secret_form([f"S1 {HYPHEN_ENDED_TOKEN}{tail}"]) == "bot_token"


@pytest.mark.parametrize(
    "text",
    [
        "a count of 12345 and a time of 9:30",
        "sk-short",
        "desk-lamp-something-long-enough",
        "-----BEGIN CERTIFICATE-----",
        "id AKIA" + "a" * 16,
    ],
)
def test_ordinary_text_is_not_refused(text: str) -> None:
    assert secret_form([text]) is None


def test_a_secret_in_any_of_the_texts_is_found() -> None:
    assert secret_form(["S1 · a reply", "sk-" + "a" * 20]) == "sk_key"
