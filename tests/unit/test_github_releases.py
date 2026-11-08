"""The GitHub adapter's payload reading and its HTTPS-only rule, without any network."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from http.client import HTTPMessage
from typing import Final

import pytest

from shikiri_yaku.adapters.github_releases import GitHubReleases, HttpsRedirects, parse_releases
from shikiri_yaku.domain.releases import Asset, Release
from shikiri_yaku.ports import ReleaseSourceError

LISTING: Final = [
    {
        "tag_name": "v0.2.0",
        "draft": False,
        "prerelease": True,
        "html_url": "https://github.com/ferrum-code-dev/shikiri-yaku/releases/tag/v0.2.0",
        "assets": [
            {
                "name": "shikiri_yaku-0.2.0-py3-none-any.whl",
                "browser_download_url": "https://github.com/x/releases/download/v0.2.0/w.whl",
                "size": 1,
            }
        ],
    }
]


def test_a_listing_is_read_and_fields_the_update_does_not_use_are_ignored() -> None:
    assert parse_releases(json.dumps(LISTING).encode()) == [
        Release(
            tag="v0.2.0",
            draft=False,
            assets=(
                Asset(
                    "shikiri_yaku-0.2.0-py3-none-any.whl",
                    "https://github.com/x/releases/download/v0.2.0/w.whl",
                ),
            ),
        )
    ]


def test_a_listing_missing_a_field_the_update_reads_is_an_error() -> None:
    without_draft = [{key: value for key, value in LISTING[0].items() if key != "draft"}]

    with pytest.raises(ReleaseSourceError, match="unexpected shape"):
        parse_releases(json.dumps(without_draft).encode())


def test_a_download_that_is_not_https_is_refused_without_a_request() -> None:
    client = GitHubReleases(timeout_s=1, max_bytes=1)

    with pytest.raises(ReleaseSourceError, match="only https"):
        client.download("http://example.invalid/shikiri_yaku-0.2.0-py3-none-any.whl")


def test_a_redirect_to_a_plain_http_address_is_refused() -> None:
    handler = HttpsRedirects()
    request = urllib.request.Request("https://example.invalid/a")

    with pytest.raises(urllib.error.URLError, match="only https"):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", HTTPMessage(), "http://example.invalid/b"
        )
