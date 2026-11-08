"""The project's releases on GitHub, read over HTTPS with the standard library."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from shikiri_yaku.domain.releases import Asset, Release
from shikiri_yaku.ports import ReleaseSourceError

if TYPE_CHECKING:
    from http.client import HTTPMessage
    from typing import IO

REPOSITORY: Final = "ferrum-code-dev/shikiri-yaku"
API_ROOT: Final = "https://api.github.com"
RELEASES_URL: Final = f"{API_ROOT}/repos/{REPOSITORY}/releases"
USER_AGENT: Final = "shikiri-yaku"
HTTPS_PREFIX: Final = "https://"


class _Asset(BaseModel):
    """A release asset as GitHub lists it; the fields the update reads, and nothing else."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    browser_download_url: str


class _Release(BaseModel):
    """A release as GitHub lists it; the fields the update reads, and nothing else."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tag_name: str
    draft: bool
    assets: list[_Asset]


def parse_releases(body: bytes) -> list[Release]:
    """Return the releases of a GitHub releases listing; a missing or mistyped field is an error."""
    try:
        parsed = TypeAdapter(list[_Release]).validate_json(body)
    except ValidationError as error:
        message = "the releases listing from GitHub has an unexpected shape"
        raise ReleaseSourceError(message) from error
    return [
        Release(
            tag=release.tag_name,
            draft=release.draft,
            assets=tuple(Asset(asset.name, asset.browser_download_url) for asset in release.assets),
        )
        for release in parsed
    ]


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an HTTPS address, so a download never leaves HTTPS."""

    def redirect_request(  # noqa: PLR0917 -- urllib's own signature, which this overrides
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith(HTTPS_PREFIX):
            message = f"refusing a redirect to {newurl}: only https is used"
            raise urllib.error.URLError(message)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubReleases:
    """Lists the releases and downloads their assets; every request has a timeout and a size cap."""

    def __init__(self, timeout_s: float, max_bytes: int) -> None:
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._opener = urllib.request.build_opener(HttpsRedirects)

    def releases(self) -> list[Release]:
        return parse_releases(self._get(RELEASES_URL, "application/vnd.github+json"))

    def download(self, url: str) -> bytes:
        return self._get(url, "application/octet-stream")

    def _get(self, url: str, accept: str) -> bytes:
        if not url.startswith(HTTPS_PREFIX):
            message = f"refusing to fetch {url}: only https is used"
            raise ReleaseSourceError(message)
        request = urllib.request.Request(  # noqa: S310 -- the scheme is checked above
            url, headers={"Accept": accept, "User-Agent": USER_AGENT}
        )
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                body: bytes = response.read(self._max_bytes + 1)
        except (OSError, ValueError) as error:
            message = f"cannot fetch {url}: {error}"
            raise ReleaseSourceError(message) from error
        if len(body) > self._max_bytes:
            message = f"{url} is larger than {self._max_bytes} bytes"
            raise ReleaseSourceError(message)
        return body
